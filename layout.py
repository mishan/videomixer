#!/usr/bin/env python3
"""Layout resolution: an operator's layout spec becomes a rectangle per source.

Pure geometry. Nothing here imports GStreamer or touches a pipeline, and that
is deliberate -- layout is the part most likely to be wrong in a way a cheap
test can catch, so it is kept where the tests need neither `gi` nor a running
pipeline to run.

A spec takes one of two shapes.

*Presets* lay out whichever sources are connected right now::

    {"preset": "grid"}                    # a near-square grid, shaped to fit
    {"preset": "grid", "cols": 3}         # ... three across, rows as needed
    {"preset": "row"}                     # side by side
    {"preset": "column"}                  # one above the other
    {"preset": "solo", "source": "cam1"}  # one full frame, the rest hidden
    {"preset": "pip", "source": "cam1"}   # one full frame, the rest inset
    {"preset": "spotlight", "source": "cam1"}   # one large, the rest in a strip

*Cells* place named sources at rectangles the caller computed itself, for the
layouts no preset covers::

    {"cells": [{"source": "cam1", "x": 0, "y": 0, "width": 640, "height": 720},
               {"source": "cam2", "x": 640, "y": 0, "width": 640, "height": 720}]}

Either form is re-resolved whenever the set of connected sources changes, which
is what lets a grid reshape itself as publishers join and drop. The difference
is what that means: a preset gives every connected source a cell, while cells
only ever touch the sources they name -- one naming a source that has not
connected yet simply places it when it arrives.

Coordinates are pixels on the output canvas by default. ``"units": "fraction"``
instead reads x/y/width/height as fractions of the canvas, so the same spec
survives a change of output resolution. ``gap`` and ``margin`` are always
pixels, because a gap that scales with the canvas is not what anyone means by
one.
"""

import collections
import math
import transition

# How a source that does not match the aspect ratio of its cell is fitted.
# `contain` scales it to fit and leaves the remainder of the cell untouched,
# so whatever is underneath shows through -- black from the mixer's base layer
# in a grid, the background source under an inset. `fill` stretches it.
CONTAIN = 'contain'
FILL = 'fill'
FITS = (CONTAIN, FILL)

PIXELS = 'pixels'
FRACTION = 'fraction'
UNITS = (PIXELS, FRACTION)

# What to do with a final row that is short of a full one: centre those cells
# under the rows above, or stretch them across. Centred is the default because
# it reads as intentional -- justifying makes the last row's cells wider than
# every other cell in the grid, which reads as a bug.
CENTER = 'center'
JUSTIFY = 'justify'
LAST_ROWS = (CENTER, JUSTIFY)

CORNERS = ('top-left', 'top-right', 'bottom-left', 'bottom-right')
POSITIONS = ('top', 'bottom', 'left', 'right')

# Keys every spec accepts, whichever shape it takes.
COMMON_KEYS = frozenset(['units', 'gap', 'margin', 'fit', 'transition'])
# ... plus these, which only mean anything to a preset.
PRESET_COMMON_KEYS = frozenset(['preset', 'order', 'exclude'])

# Preset name -> the keys it accepts on top of the common ones.
PRESET_KEYS = {
    'grid': frozenset(['rows', 'cols', 'last_row']),
    'row': frozenset(),
    'column': frozenset(),
    'solo': frozenset(['source']),
    'pip': frozenset(['source', 'size', 'corner']),
    'spotlight': frozenset(['source', 'size', 'position']),
}

# Spellings an operator is likely to reach for. One per concept: a second
# spelling of the same alias is API surface nobody documents and everybody
# has to keep working.
PRESET_ALIASES = {
    'side-by-side': 'row',
    'horizontal': 'row',
    'stacked': 'column',
    'vertical': 'column',
    'fullscreen': 'solo',
}

PRESETS = tuple(sorted(PRESET_KEYS))

CELL_KEYS = frozenset(['source', 'x', 'y', 'width', 'height', 'z', 'fit',
                       'alpha'])

DEFAULT_PIP_SIZE = 0.25
DEFAULT_SPOTLIGHT_SIZE = 0.25


class LayoutError(ValueError):
    """A layout spec that cannot be resolved."""


class Cell(collections.namedtuple(
        'Cell', 'x y width height zorder fit alpha')):
    """Where one source goes on the canvas.

    `zorder` is the caller-facing z (the background is 0), matching what the
    move endpoint takes. `alpha` of 0 is how a preset hides a source it has no
    room for: the branch keeps running and keeps its mixer pad, so bringing it
    back is a property change rather than a reconnect.
    """

    __slots__ = ()

    def as_dict(self):
        return {'x': self.x, 'y': self.y,
                'width': self.width, 'height': self.height,
                'z': self.zorder, 'fit': self.fit, 'alpha': self.alpha}


_Rect = collections.namedtuple('_Rect', 'x y width height')


# -- entry point -----------------------------------------------------------

def resolve(spec, canvas_width, canvas_height, source_ids):
    """Resolve a spec against a canvas and the connected sources.

    Returns an ordered {source_id: Cell}. Sources the spec does not place are
    absent from it rather than present with some default, so a caller can tell
    "leave this one alone" from "put this one here".
    """
    if not isinstance(spec, dict):
        raise LayoutError('layout must be a JSON object')
    if 'preset' in spec and 'cells' in spec:
        raise LayoutError('layout takes either "preset" or "cells", not both')

    try:
        transition.settings(spec)
    except ValueError as exc:
        raise LayoutError(str(exc)) from exc

    units = _one_of(spec, 'units', UNITS, PIXELS)
    fit = _one_of(spec, 'fit', FITS, CONTAIN)
    gap = _non_negative(spec, 'gap', 0)
    margin = _non_negative(spec, 'margin', 0)

    canvas = _inset(_Rect(0, 0, canvas_width, canvas_height), margin)

    if 'cells' in spec:
        _check_keys(spec, COMMON_KEYS | frozenset(['cells']), 'layout')
        return _explicit(spec['cells'], canvas, units, fit, source_ids)

    if 'preset' not in spec:
        raise LayoutError('layout needs either "preset" or "cells"')

    name = spec['preset']
    if not isinstance(name, str):
        raise LayoutError('preset must be a string')
    name = PRESET_ALIASES.get(name, name)
    if name not in PRESET_KEYS:
        raise LayoutError('unknown preset "{}" -- known presets are {}'.format(
            spec['preset'], ', '.join(PRESETS)))
    _check_keys(spec, COMMON_KEYS | PRESET_COMMON_KEYS | PRESET_KEYS[name],
                'preset "{}"'.format(name))

    ids = _participants(spec, source_ids)
    if not ids:
        return collections.OrderedDict()

    if name == 'grid':
        rows, cols = _grid_shape(len(ids),
                                 _positive(spec, 'rows', None),
                                 _positive(spec, 'cols', None))
        last_row = _one_of(spec, 'last_row', LAST_ROWS, CENTER)
        return _grid(ids, canvas, rows, cols, gap, fit, last_row)
    if name == 'row':
        return _grid(ids, canvas, 1, len(ids), gap, fit, JUSTIFY)
    if name == 'column':
        return _grid(ids, canvas, len(ids), 1, gap, fit, JUSTIFY)
    if name == 'solo':
        return _solo(ids, _subject(spec, ids), canvas, fit)
    if name == 'pip':
        return _pip(ids, _subject(spec, ids), canvas, fit, gap,
                    _fraction(spec, 'size', DEFAULT_PIP_SIZE),
                    _one_of(spec, 'corner', CORNERS, 'bottom-right'))
    return _spotlight(ids, _subject(spec, ids), canvas, fit, gap,
                      _fraction(spec, 'size', DEFAULT_SPOTLIGHT_SIZE),
                      _one_of(spec, 'position', POSITIONS, 'bottom'))


# -- shapes ----------------------------------------------------------------

def _grid(ids, canvas, rows, cols, gap, fit, last_row):
    cells = collections.OrderedDict()
    bands = _spans(canvas.y, canvas.height, rows, gap, 'height')
    for row, (y, height) in enumerate(bands):
        row_ids = ids[row * cols:(row + 1) * cols]
        if not row_ids:
            break
        if len(row_ids) == cols or last_row == JUSTIFY:
            columns = _spans(canvas.x, canvas.width, len(row_ids), gap, 'width')
        else:
            columns = _centered(canvas.x, canvas.width, len(row_ids), cols, gap)
        for source_id, (x, width) in zip(row_ids, columns):
            cells[source_id] = Cell(x, y, width, height, 1, fit, 1.0)
    return cells


def _solo(ids, subject, canvas, fit):
    cells = collections.OrderedDict()
    cells[subject] = Cell(canvas.x, canvas.y, canvas.width, canvas.height,
                          1, fit, 1.0)
    # Everything else keeps its pad and its running branch and simply stops
    # being drawn, so switching back is a property change, not a reconnect.
    for source_id in ids:
        if source_id != subject:
            cells[source_id] = Cell(canvas.x, canvas.y, canvas.width,
                                    canvas.height, 1, fit, 0.0)
    return cells


def _pip(ids, subject, canvas, fit, gap, size, corner):
    cells = collections.OrderedDict()
    cells[subject] = Cell(canvas.x, canvas.y, canvas.width, canvas.height,
                          1, fit, 1.0)

    insets = [i for i in ids if i != subject]
    if not insets:
        return cells

    # Scaling both axes by the same fraction gives insets the aspect ratio of
    # the canvas, which is what a PiP is expected to look like.
    width = max(1, int(round(canvas.width * size)))
    height = max(1, int(round(canvas.height * size)))

    # Enough insets at the requested size would march straight off the edge --
    # the fifth inset of a default-sized PiP on a 1280px canvas starts at a
    # negative x and is simply not on screen. Shrink them to fit instead, for
    # the same reason a grid grows a row rather than truncating: no layout may
    # make a connected publisher vanish.
    room = canvas.width - gap * (len(insets) + 1)
    if room < len(insets):
        raise LayoutError(
            'cannot fit {} inset(s) with a {}px gap into a {}px canvas'.format(
                len(insets), gap, canvas.width))
    if width * len(insets) > room:
        shrink = room / (width * len(insets))
        width = max(1, int(width * shrink))
        height = max(1, int(height * shrink))

    top = corner.startswith('top')
    left = corner.endswith('left')

    y = canvas.y + gap if top else canvas.y + canvas.height - gap - height
    for index, source_id in enumerate(insets):
        step = index * (width + gap)
        # Additional insets march inward from the chosen corner along the edge.
        x = (canvas.x + gap + step if left
             else canvas.x + canvas.width - gap - width - step)
        cells[source_id] = Cell(x, y, width, height, 2 + index, fit, 1.0)
    return cells


def _spotlight(ids, subject, canvas, fit, gap, size, position):
    others = [i for i in ids if i != subject]
    if not others:
        return _solo(ids, subject, canvas, fit)

    vertical = position in ('top', 'bottom')
    extent = canvas.height if vertical else canvas.width
    strip = max(1, int(round(extent * size)))
    if strip + gap >= extent:
        raise LayoutError(
            'spotlight size {} leaves no room for the main source in a '
            '{}px canvas'.format(size, extent))
    main = extent - strip - gap

    if vertical:
        main_y = canvas.y + strip + gap if position == 'top' else canvas.y
        strip_y = canvas.y if position == 'top' else canvas.y + main + gap
        subject_rect = _Rect(canvas.x, main_y, canvas.width, main)
        strip_rect = _Rect(canvas.x, strip_y, canvas.width, strip)
        rows, cols = 1, len(others)
    else:
        main_x = canvas.x + strip + gap if position == 'left' else canvas.x
        strip_x = canvas.x if position == 'left' else canvas.x + main + gap
        subject_rect = _Rect(main_x, canvas.y, main, canvas.height)
        strip_rect = _Rect(strip_x, canvas.y, strip, canvas.height)
        rows, cols = len(others), 1

    cells = collections.OrderedDict()
    cells[subject] = Cell(subject_rect.x, subject_rect.y, subject_rect.width,
                          subject_rect.height, 1, fit, 1.0)
    cells.update(_grid(others, strip_rect, rows, cols, gap, fit, JUSTIFY))
    return cells


def _explicit(raw_cells, canvas, units, default_fit, source_ids):
    if not isinstance(raw_cells, list):
        raise LayoutError('"cells" must be a list')
    connected = set(source_ids)
    cells = collections.OrderedDict()
    for index, raw in enumerate(raw_cells):
        where = 'cells[{}]'.format(index)
        if not isinstance(raw, dict):
            raise LayoutError('{} must be an object'.format(where))
        _check_keys(raw, CELL_KEYS, where)

        source_id = raw.get('source')
        if not source_id or not isinstance(source_id, str):
            raise LayoutError('{} needs a "source"'.format(where))
        if source_id in cells:
            raise LayoutError(
                '{} places "{}" twice'.format(where, source_id))
        for key in ('width', 'height'):
            if key not in raw:
                raise LayoutError('{} needs a "{}"'.format(where, key))

        x = _scaled(raw, 'x', 0, canvas.width, units, where)
        y = _scaled(raw, 'y', 0, canvas.height, units, where)
        width = _scaled(raw, 'width', None, canvas.width, units, where)
        height = _scaled(raw, 'height', None, canvas.height, units, where)
        if width < 1 or height < 1:
            raise LayoutError(
                '{} resolves to {}x{}; a cell needs a positive size'.format(
                    where, width, height))

        fit = _one_of(raw, 'fit', FITS, default_fit)
        alpha = _fraction(raw, 'alpha', 1.0, allow_zero=True)
        zorder = raw.get('z', 1)
        if not isinstance(zorder, int) or isinstance(zorder, bool) or zorder < 0:
            raise LayoutError('{} "z" must be a non-negative integer'.format(where))

        # A cell may name a source that has not connected yet. Holding the
        # place rather than rejecting it is what lets an operator describe the
        # whole show up front and have each camera land in its slot as it
        # comes up.
        if source_id in connected:
            cells[source_id] = Cell(canvas.x + x, canvas.y + y, width, height,
                                    zorder, fit, alpha)
    return cells


# -- geometry helpers ------------------------------------------------------

def _spans(start, extent, count, gap, axis):
    """Offsets and sizes for `count` cells tiling `extent`, separated by `gap`.

    Every edge is computed from the total rather than by accumulating a rounded
    cell size, so the cells tile the extent exactly and no seam opens up on the
    last column of a 3-up.
    """
    content = extent - gap * (count - 1)
    if content < count:
        raise LayoutError(
            'cannot fit {} cells with a {}px gap into {}px of {}'.format(
                count, gap, extent, axis))
    spans = []
    for index in range(count):
        low = (content * index) // count
        high = (content * (index + 1)) // count
        spans.append((start + low + index * gap, high - low))
    return spans


def _centered(start, extent, count, of, gap):
    """`count` cells the size they would be in a full row of `of`, centred."""
    size = (extent - gap * (of - 1)) // of
    if size < 1:
        raise LayoutError(
            'cannot fit {} cells with a {}px gap into {}px'.format(
                of, gap, extent))
    occupied = count * size + gap * (count - 1)
    offset = start + (extent - occupied) // 2
    return [(offset + index * (size + gap), size) for index in range(count)]


def _inset(rect, margin):
    if margin * 2 >= min(rect.width, rect.height):
        raise LayoutError(
            'margin {}px leaves nothing of a {}x{} canvas'.format(
                margin, rect.width, rect.height))
    return _Rect(rect.x + margin, rect.y + margin,
                 rect.width - margin * 2, rect.height - margin * 2)


def _grid_shape(count, rows, cols):
    """Rows and columns for `count` sources.

    `rows` and `cols` are floors, not caps: a grid asked for 3 columns and
    handed 7 sources grows a third row rather than dropping anyone. Nothing an
    operator does to the layout should make a live publisher disappear.
    """
    if rows and cols:
        while rows * cols < count:
            rows += 1
    elif cols:
        rows = max(1, math.ceil(count / cols))
    elif rows:
        cols = max(1, math.ceil(count / rows))
    else:
        cols = max(1, math.ceil(math.sqrt(count)))
        rows = max(1, math.ceil(count / cols))
    return rows, cols


# -- spec reading ----------------------------------------------------------

def _participants(spec, source_ids):
    """The sources a preset lays out, in the order it lays them out.

    `order` pins the leading positions; anything else follows in the order it
    was added. Naming a source that is not connected is not an error -- it
    takes its place the moment it arrives.
    """
    exclude = _string_list(spec, 'exclude')
    order = _string_list(spec, 'order')
    connected = list(source_ids)
    ordered = [i for i in order if i in connected]
    ordered += [i for i in connected if i not in set(order)]
    return [i for i in ordered if i not in set(exclude)]


def _subject(spec, ids):
    """The source a solo/pip/spotlight builds itself around."""
    source_id = spec.get('source')
    if source_id is None:
        return ids[0]
    if not isinstance(source_id, str):
        raise LayoutError('"source" must be a string')
    if source_id not in ids:
        raise LayoutError(
            '"source": "{}" is not a source of this stream'.format(source_id))
    return source_id


def _check_keys(spec, allowed, where):
    unknown = sorted(set(spec) - set(allowed))
    if unknown:
        raise LayoutError('{} does not take {} (accepts {})'.format(
            where, ', '.join('"{}"'.format(k) for k in unknown),
            ', '.join(sorted(allowed))))


def _one_of(spec, key, choices, default):
    value = spec.get(key, default)
    if value not in choices:
        raise LayoutError('"{}" must be one of {}, not {!r}'.format(
            key, ', '.join(choices), value))
    return value


def _number(spec, key, default, where='layout'):
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LayoutError('{} "{}" must be a number, not {!r}'.format(
            where, key, value))
    return value


def _non_negative(spec, key, default):
    value = _number(spec, key, default)
    if value < 0:
        raise LayoutError('"{}" cannot be negative'.format(key))
    return int(value)


def _positive(spec, key, default):
    if key not in spec:
        return default
    value = _number(spec, key, default)
    if int(value) != value or value < 1:
        raise LayoutError('"{}" must be a positive whole number'.format(key))
    return int(value)


def _fraction(spec, key, default, allow_zero=False):
    value = _number(spec, key, default)
    if not 0 <= value <= 1 or (value == 0 and not allow_zero):
        raise LayoutError('"{}" must be a fraction between {} and 1'.format(
            key, '0' if allow_zero else 'just above 0'))
    return float(value)


def _string_list(spec, key):
    value = spec.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise LayoutError('"{}" must be a list of source ids'.format(key))
    return value


def _scaled(raw, key, default, extent, units, where):
    """Read a coordinate, converting a fraction of the canvas into pixels."""
    if key not in raw and default is not None:
        return default
    value = _number(raw, key, default, where)
    if units == FRACTION:
        if not -1 <= value <= 1:
            raise LayoutError(
                '{} "{}" is {} -- with "units": "fraction" it has to be a '
                'fraction of the canvas'.format(where, key, value))
        return int(round(value * extent))
    if int(value) != value:
        raise LayoutError(
            '{} "{}" must be a whole number of pixels (use "units": '
            '"fraction" for proportions)'.format(where, key))
    return int(value)
