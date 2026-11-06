"""Run with js-check-venv; pending network replies model real browser races."""
import json
from pathlib import Path
import unittest

import quickjs


SCRIPT = Path(__file__).with_name('model_preview.js').read_text()


class Browser:
    def __init__(self, secure=True):
        self.ctx = quickjs.Context()
        self.ctx.eval('''
const elements = {}, documentEvents = {}, windowEvents = {}, timers = [];
let requests = [];
function element(id) {
  if (!elements[id]) elements[id] = {disabled: false, textContent: '', value: '',
    addEventListener(kind, fn) {this[kind] = fn;}};
  return elements[id];
}
const document = {hidden: false, getElementById: element,
  addEventListener(kind, fn) {documentEvents[kind] = fn;}};
const window = {addEventListener(kind, fn) {windowEvents[kind] = fn;}};
var crypto = {randomUUID: () => 'fake-browser-uuid'};
const AbortSignal = {timeout: () => null};
function setInterval(fn) {timers.push(fn); return timers.length;}
function clearInterval(id) {timers[id - 1] = null;}
function tick() {for (const timer of timers) if (timer) timer();}
function fetch(path, options) {
  return new Promise((resolve, reject) => requests.push({path,
    body: options.body ? JSON.parse(options.body) : null,
    keepalive: !!options.keepalive, resolve, reject, settled: false}));
}
function resolveRequest(index, data, ok = true) {
  requests[index].settled = true;
  requests[index].resolve({ok, json: () => Promise.resolve(data)});
}
function rejectRequest(index) {
  requests[index].settled = true;
  requests[index].reject(new Error('old heartbeat timed out'));
}
element('model-task').value = 'put block in bowl';
element('model-arm').value = 'left';
element('model-profile').value = 'qwen3_5_2b';
element('model-steps').value = '4';
''')
        if not secure:
            self.ctx.eval('crypto = {};')
        self.ctx.eval(SCRIPT)

    def eval(self, code):
        result = self.ctx.eval(code)
        self.drain()
        return result

    def drain(self):
        for _ in range(1000):
            if not self.ctx.execute_pending_job():
                return
        raise AssertionError('JS jobs failed to settle')

    def requests(self, path=None):
        requests = json.loads(self.ctx.eval('JSON.stringify(requests)'))
        return [(index, request) for index, request in enumerate(requests)
                if path is None or request['path'] == path]

    def respond(self, index, data=None):
        self.eval(f'resolveRequest({index}, {json.dumps(data or {"ok": True})});')

    def start_reply(self, index, run_id):
        self.respond(index, {'ok': True, 'run_id': run_id, 'model_run': {
            'active': True, 'thread_pending': True, 'run_id': run_id,
            'arm': 'left', 'steps': 0, 'max_steps': 4, 'message': 'running'}})

    def start(self, run_id='run-a'):
        self.eval("elements['model-start'].click();")
        index = self.requests('/api/model/start')[-1][0]
        self.start_reply(index, run_id)
        return index

    def settle_stops(self):
        # A late start can create a second stop request to revoke its returned run ID.
        for _ in range(5):
            pending = [(i, r) for i, r in self.requests('/api/model/stop') if not r['settled']]
            if not pending:
                return
            for index, _ in pending:
                self.respond(index, {'ok': True, 'message': 'stopped'})
        raise AssertionError('Stop requests did not settle')


class ModelPageLifecycle(unittest.TestCase):
    def test_completed_run_restart_resets_counter_and_ignores_old_completion(self):
        browser = Browser()
        browser.start('finished-run')
        old = {'active': False, 'thread_pending': False, 'run_id': 'finished-run',
               'arm': 'left', 'steps': 50, 'max_steps': 50, 'message': '已达到本次步数上限',
               'raw_token': 'MV_DOWN', 'execution_token': 'MV_DOWN'}
        browser.eval('window.renderModelState('+json.dumps(old)+');')
        browser.eval("elements['model-steps'].value='50'; elements['model-start'].click();")
        self.assertIn('0/50', browser.ctx.eval("elements['model-result'].textContent"))
        browser.eval('window.renderModelState('+json.dumps(old)+');')
        self.assertIn('0/50', browser.ctx.eval("elements['model-result'].textContent"))
        index = browser.requests('/api/model/start')[-1][0]
        browser.start_reply(index, 'fresh-run')
        browser.eval('window.renderModelState('+json.dumps(old)+');')
        text = browser.ctx.eval("elements['model-result'].textContent")
        self.assertIn('0/', text)
        self.assertNotIn('MV_DOWN', text)
        browser.eval('tick();')
        self.assertEqual(browser.requests('/api/model/heartbeat')[-1][1]['body']['run_id'], 'fresh-run')

    def test_lan_http_without_random_uuid_still_initializes_controls(self):
        browser = Browser(secure=False)
        browser.start()
        request = browser.requests('/api/model/start')[0][1]
        self.assertTrue(request['body']['client'].startswith('model-'))
        browser.eval('tick();')
        self.assertEqual(len(browser.requests('/api/model/heartbeat')), 1)

    def test_preview_never_starts_or_heartbeats_a_motion_run(self):
        browser = Browser()
        browser.eval("elements['model-preview'].submit({preventDefault() {}});")
        browser.eval("elements['model-step'].click(); elements['model-start'].click(); tick();")
        self.assertEqual([r['path'] for _, r in browser.requests()], ['/api/model/preview'])
        browser.respond(0, {'ok': True, 'arm': 'left', 'token': 'MV_UP', 'elapsed_s': .2})
        browser.eval('tick();')
        self.assertEqual(len(browser.requests()), 1)
        self.assertIn('仅预览', browser.ctx.eval("elements['model-result'].textContent"))

    def test_stop_while_start_is_pending_revokes_late_success(self):
        browser = Browser()
        browser.eval("elements['model-start'].click(); elements['model-stop'].click();")
        browser.settle_stops()
        browser.start_reply(0, 'late-run')
        browser.settle_stops()
        browser.eval('tick(); tick();')
        self.assertGreaterEqual(len(browser.requests('/api/model/stop')), 1)
        self.assertEqual(browser.requests('/api/model/heartbeat'), [])
        # Either abort the pending request or revoke the run when its ID arrives.
        self.assertFalse(browser.ctx.eval("elements['model-task'].disabled"))

    def test_hidden_tab_during_pending_start_never_keeps_run_alive(self):
        browser = Browser()
        browser.eval("elements['model-step'].click(); document.hidden = true; documentEvents.visibilitychange();")
        browser.settle_stops()
        browser.start_reply(0, 'hidden-run')
        browser.settle_stops()
        browser.eval('tick();')
        stops = browser.requests('/api/model/stop')
        self.assertTrue(stops)
        self.assertTrue(all(request['keepalive'] for _, request in stops))
        self.assertEqual(browser.requests('/api/model/heartbeat'), [])

    def test_pagehide_stops_an_active_run_without_next_heartbeat(self):
        browser = Browser()
        browser.start()
        browser.eval('windowEvents.pagehide();')
        browser.settle_stops()
        browser.eval('tick();')
        self.assertEqual(browser.requests('/api/model/heartbeat'), [])
        self.assertEqual(len(browser.requests('/api/model/stop')), 1)

    def test_pending_stop_blocks_new_work_and_targets_the_previous_run(self):
        browser = Browser()
        browser.start('run-a')
        browser.eval("elements['model-stop'].click(); elements['model-start'].click(); elements['model-preview'].submit({preventDefault() {}});")
        self.assertEqual(len(browser.requests('/api/model/start')), 1)
        self.assertEqual(browser.requests('/api/model/preview'), [])
        self.assertEqual(browser.requests('/api/model/stop')[-1][1]['body'], {'run_id': 'run-a'})
        browser.settle_stops()
        browser.start('run-b')
        browser.eval('tick();')
        self.assertEqual(browser.requests('/api/model/heartbeat')[-1][1]['body']['run_id'], 'run-b')

    def test_done_stops_heartbeats_and_releases_form(self):
        browser = Browser()
        browser.start()
        browser.eval('tick();')
        heartbeat = browser.requests('/api/model/heartbeat')[-1][0]
        browser.respond(heartbeat)
        browser.eval("window.renderModelState({active:false, thread_pending:false, run_id:'run-a', phase:'done', message:'DONE'}); tick(); tick();")
        self.assertEqual(len(browser.requests('/api/model/heartbeat')), 1)
        self.assertFalse(browser.ctx.eval("elements['model-start'].disabled"))
        self.assertFalse(browser.ctx.eval("elements['model-task'].disabled"))

    def test_old_inactive_state_cannot_revoke_new_run_heartbeats(self):
        browser = Browser()
        browser.start('new-run')
        browser.eval("window.renderModelState({active:false, thread_pending:false, run_id:'old-run', phase:'done', message:'old DONE'}); tick();")
        heartbeats = browser.requests('/api/model/heartbeat')
        self.assertTrue(heartbeats, 'stale state response revoked a newer run')
        self.assertEqual(heartbeats[-1][1]['body']['run_id'], 'new-run')
        self.assertTrue(browser.ctx.eval("elements['model-start'].disabled"))

    def test_old_heartbeat_failure_does_not_stop_a_new_run(self):
        browser = Browser()
        browser.start('run-a')
        browser.eval('tick();')
        old_heartbeat = browser.requests('/api/model/heartbeat')[-1][0]
        browser.eval("elements['model-stop'].click();")
        browser.settle_stops()
        browser.start('run-b')
        stop_count = len(browser.requests('/api/model/stop'))
        browser.eval(f'rejectRequest({old_heartbeat});')
        self.assertEqual(len(browser.requests('/api/model/stop')), stop_count,
                         'old heartbeat failure sent a stop for the new run')
        browser.eval('tick();')
        heartbeats = browser.requests('/api/model/heartbeat')
        self.assertEqual(heartbeats[-1][1]['body']['run_id'], 'run-b')

    def test_pending_start_stop_restart_does_not_let_old_response_own_new_run(self):
        browser = Browser()
        browser.eval("elements['model-start'].click(); elements['model-stop'].click();")
        browser.settle_stops()
        browser.eval("elements['model-start'].click();")
        starts = browser.requests('/api/model/start')
        if len(starts) == 1:
            # Also valid: keep restart blocked until the cancelled start has settled.
            browser.start_reply(starts[0][0], 'old-run')
            browser.settle_stops()
            browser.start('new-run')
        else:
            browser.start_reply(starts[0][0], 'old-run')
            browser.settle_stops()
            browser.start_reply(starts[1][0], 'new-run')
        stop_count = len(browser.requests('/api/model/stop'))
        browser.eval('tick();')
        heartbeats = browser.requests('/api/model/heartbeat')
        self.assertTrue(heartbeats, 'new run lost ownership after old start response')
        self.assertEqual(heartbeats[-1][1]['body']['run_id'], 'new-run')
        self.assertEqual(len(browser.requests('/api/model/stop')), stop_count)


class ModelSwitchBrowserTests(unittest.TestCase):
    def test_switch_posts_selected_profile_and_blocks_inference(self):
        browser = Browser()
        browser.eval("element('model-profile').value = 'qwen3_5_4b'; elements['model-load'].click();")
        request = browser.requests('/api/model/switch')[0]
        self.assertEqual(request[1]['body'], {'profile': 'qwen3_5_4b'})
        browser.respond(request[0], {'ok': True, 'model_service': {
            'phase': 'loading', 'profile': 'qwen3_5_4b', 'message': 'loading'}})
        browser.eval("elements['model-step'].click();")
        self.assertFalse(browser.requests('/api/model/start'))
        browser.eval("window.renderModelService({phase:'ready', profile:'qwen3_5_4b', message:'ready'});")
        browser.eval("window.renderModelState({active:false, thread_pending:false}); elements['model-step'].click();")
        self.assertEqual(browser.requests('/api/model/start')[0][1]['body']['profile'], 'qwen3_5_4b')


if __name__ == '__main__':
    unittest.main()
