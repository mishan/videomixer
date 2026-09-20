"""Pure keyframe planning for compositor layout transitions."""

import math

PROPERTIES = ('xpos', 'ypos', 'width', 'height', 'alpha')
STEPS = 20
DEFAULT_DURATION = 0.4
MAX_DURATION = 2.0
DEFAULT_EASING = 'ease-in-out'
EASINGS = ('linear', 'ease-in', 'ease-out', 'ease-in-out')


def settings(spec):
    """Validate a layout's optional transition and return its settings."""
    if 'transition' not in spec:
        return None
    value = spec['transition']
    if not isinstance(value, dict):
        raise ValueError('transition must be an object')
    unknown = set(value) - {'duration', 'easing'}
    if unknown:
        raise ValueError('unknown transition option(s): {}'.format(
            ', '.join(sorted(unknown))))
    duration = value.get('duration', DEFAULT_DURATION)
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 0 < duration <= MAX_DURATION
            or not math.isfinite(duration)):
        raise ValueError('transition duration must be greater than 0 and at most 2 seconds')
    easing = value.get('easing', DEFAULT_EASING)
    if easing not in EASINGS:
        raise ValueError('transition easing must be one of {}'.format(
            ', '.join(EASINGS)))
    return float(duration), easing


def _ease(fraction, easing):
    if easing == 'linear':
        return fraction
    if easing == 'ease-in':
        return fraction ** 3
    if easing == 'ease-out':
        return 1 - (1 - fraction) ** 3
    return (4 * fraction ** 3 if fraction < 0.5
            else 1 - ((-2 * fraction + 2) ** 3) / 2)


def plan(from_cells, to_cells, duration, easing):
    """Return {source: {pad property: [(seconds, value), ...]}}.

    A missing starting cell is a new source: it appears at its destination
    rectangle with alpha zero and fades in. Existing cells can be snapshots of
    the actual compositor pad, including an interrupted interpolation.
    """
    result = {}
    for source_id, cell in to_cells.items():
        end = {'xpos': cell.x, 'ypos': cell.y, 'width': cell.width,
               'height': cell.height, 'alpha': cell.alpha}
        start = from_cells.get(source_id)
        if start is None:
            start = dict(end, alpha=0.0)
        result[source_id] = {
            prop: [(duration * step / STEPS,
                    float(start[prop] + (end[prop] - start[prop])
                          * _ease(step / STEPS, easing)))
                   for step in range(STEPS + 1)]
            for prop in PROPERTIES if start[prop] != end[prop]
        }
    return result
