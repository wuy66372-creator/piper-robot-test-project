"""Versioned pre-action gripper context shared by dataset conversion and serving."""
import math

CONTRACT = 'width-last-command-v1'
COMMANDS = ('GRASP', 'RELEASE', 'unknown')


def state(width=None, command='unknown'):
    if command not in COMMANDS:
        raise ValueError('Invalid last gripper command')
    if width is not None:
        if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or not 0 <= width <= .1002:
            raise ValueError('Invalid measured gripper width')
        width = float(width)
    return {'gripper_width': width, 'last_gripper_command': command}


def from_observation(obs):
    return state(obs.get('gripper_width'), obs.get('last_gripper_command', 'unknown'))


class ScopedCommands:
    """One run/episode only. Never imports transport-level command memory."""
    def __init__(self):
        self.last = dict(left='unknown', right='unknown')

    def observe(self, side, obs):
        return state(obs.get('gripper_width'), self.last[side])

    def completed(self, tokens, success=True, auto_released=False):
        for side, token in tokens.items():
            if auto_released and success:
                self.last[side]='RELEASE'
            elif token in ('GRASP', 'RELEASE'):
                self.last[side] = ('RELEASE' if auto_released else token) if success else 'unknown'


def render(value, label='Gripper'):
    value = from_observation(value)
    width = 'unknown' if value['gripper_width'] is None else f"{value['gripper_width'] * 1000:.3f}"
    return (f"{label} measured width (mm): {width}\n"
            f"{label} last command: {value['last_gripper_command']}")


def add_context(prompt, values, dual=False):
    marker = '\nLeft arm recent moves,' if dual else '\nRecent moves,'
    if marker not in prompt:
        raise ValueError('Unsupported gripper context prompt')
    text = ('\n'.join(render(values[s], f'{s.capitalize()} gripper') for s in ('left', 'right'))
            if dual else render(values))
    # No object-holding inference: measured width and last command are separate facts.
    return prompt.replace(marker, '\n' + text + marker, 1)


def episode_states(rows):
    """Old episodes reconstruct commands strictly from preceding actions, never labels ahead."""
    last = dict(left='unknown', right='unknown')
    before = []
    for row in rows:
        # Rebuild within this episode even if old rows contain global command memory.
        before.append({s: state(row[s].get('gripper_width'), last[s]) for s in last})
        for side in last:
            token = row[side]['token']
            if token in ('GRASP', 'RELEASE'):
                last[side] = token if row.get('execution_status', 'succeeded') == 'succeeded' else 'unknown'
    return before, last
