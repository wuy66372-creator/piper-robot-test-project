"""No hardware: validate recording mode defaults and parse the complete page JS."""
import json
from pathlib import Path
import re
import shutil
import subprocess

html=(Path(__file__).parent/'gumi_index.html').read_text()
program=[]
for script in re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>',html,re.S):
    program.append('new Function('+json.dumps(script)+');')
program.append('''
const assert=require('node:assert/strict');
let STATE={}, stops=0, clears=0;
const control={checked:false};
function $(id){return control;}
function liveStop(){stops++;}
function clearQueue(){clears++;}
''')
start=html.index('function syncRecordingMode(st) {')
end=html.index('function render(st) {',start)
program.append(html[start:end])
program.append('syncRecordingMode({recording:true});STATE={recording:true};')
program.append('assert(control.checked && stops===1 && clears===1);')
# Polls must not reset an explicit choice to use COMMIT instead.
program.append('control.checked=false;syncRecordingMode({recording:true});')
program.append('assert(!control.checked && stops===1);')
program.append('syncRecordingMode({recording:false});STATE={recording:false};syncRecordingMode({recording:true});')
program.append('assert(control.checked && stops===3 && clears===3);')
subprocess.run([shutil.which('node') or 'node','-e','\n'.join(program)],check=True)
print('Collection JS passed: default held-key mode, preserved choice, new session reset and syntax.')
