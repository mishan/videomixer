"""Layout resolution.

Pure geometry, so unlike the rest of the suite these need neither GStreamer nor
a pipeline. The invariants worth holding onto are that cells tile their canvas
exactly (a seam on the right-hand column of a 3-up is visible on air), that no
layout can make a connected source disappear by accident, and that a bad spec
is rejected before anything moves.
"""

import pytest

import layout


def rects(cells):
    return {k: (c.x, c.y, c.width, c.height) for k, c in cells.items()}


def ids(count):
    return ['s{}'.format(i) for i in range(count)]


def covers_exactly(cells, width, height):
    """Whether the cells tile the canvas with no overlap and nothing left over."""
    painted = set()
    for cell in cells.values():
        for x in range(cell.x, cell.x + cell.width):
            for y in range(cell.y, cell.y + cell.height):
                if (x, y) in painted:
                    return False
                painted.add((x, y))
    return len(painted) == width * height


# -- grid ------------------------------------------------------------------

@pytest.mark.parametrize('count,shape', [
    (1, (1, 1)),
    (2, (1, 2)),
    (3, (2, 2)),
    (4, (2, 2)),
    (5, (2, 3)),
    (6, (2, 3)),
    (9, (3, 3)),
    (10, (3, 4)),
])
def test_grid_shape_stays_near_square(count, shape):
    assert layout._grid_shape(count, None, None) == shape


def test_grid_of_two_is_side_by_side():
    cells = layout.resolve({'preset': 'grid'}, 1280, 720, ids(2))
    assert rects(cells) == {'s0': (0, 0, 640, 720), 's1': (640, 0, 640, 720)}


def test_grid_of_nine_tiles_the_canvas_exactly():
    cells = layout.resolve({'preset': 'grid'}, 90, 90, ids(9))
    assert covers_exactly(cells, 90, 90)


def test_grid_tiles_exactly_even_when_the_canvas_does_not_divide():
    """1280/3 is not a whole number; the columns still have to meet.

    Each edge is computed from the canvas total rather than by accumulating a
    rounded cell width, which is what stops a one-pixel seam appearing down the
    right-hand side.
    """
    cells = layout.resolve({'preset': 'grid', 'cols': 3}, 1280, 720, ids(3))
    assert covers_exactly(cells, 1280, 720)
    assert sum(c.width for c in cells.values()) == 1280


def test_grid_centers_a_short_last_row():
    cells = layout.resolve({'preset': 'grid'}, 1280, 720, ids(3))
    assert rects(cells)['s2'] == (320, 360, 640, 360)


def test_grid_can_justify_a_short_last_row_instead():
    cells = layout.resolve({'preset': 'grid', 'last_row': 'justify'},
                           1280, 720, ids(3))
    assert rects(cells)['s2'] == (0, 360, 1280, 360)


def test_grid_columns_are_a_floor_not_a_cap():
    """Asking for 3 columns and handing over 7 sources grows a row.

    Nothing an operator does to the layout should drop a live publisher off
    the screen, so the grid grows rather than truncating.
    """
    cells = layout.resolve({'preset': 'grid', 'cols': 3}, 1280, 720, ids(7))
    assert len(cells) == 7
    assert len({c.y for c in cells.values()}) == 3


def test_grid_rows_alone_fixes_the_row_count():
    cells = layout.resolve({'preset': 'grid', 'rows': 1}, 1280, 720, ids(4))
    assert len({c.y for c in cells.values()}) == 1
    assert len(cells) == 4


def test_gap_and_margin_come_out_of_the_cells():
    cells = layout.resolve({'preset': 'grid', 'cols': 2, 'gap': 10,
                            'margin': 20}, 1000, 500, ids(2))
    assert rects(cells) == {'s0': (20, 20, 475, 460),
                            's1': (505, 20, 475, 460)}


def test_grid_reshapes_when_a_source_joins():
    """The same spec, one more source: the 3-up becomes a 2x2 on its own."""
    spec = {'preset': 'grid'}
    before = layout.resolve(spec, 1280, 720, ids(3))
    after = layout.resolve(spec, 1280, 720, ids(4))
    assert len({c.y for c in before.values()}) == 2
    assert rects(after)['s2'] == (0, 360, 640, 360)
    assert covers_exactly(after, 1280, 720)


# -- the other presets -----------------------------------------------------

def test_row_and_column():
    row = layout.resolve({'preset': 'row'}, 1200, 600, ids(3))
    assert rects(row) == {'s0': (0, 0, 400, 600), 's1': (400, 0, 400, 600),
                          's2': (800, 0, 400, 600)}
    column = layout.resolve({'preset': 'column'}, 1200, 600, ids(2))
    assert rects(column) == {'s0': (0, 0, 1200, 300),
                             's1': (0, 300, 1200, 300)}


@pytest.mark.parametrize('alias,preset', sorted(layout.PRESET_ALIASES.items()))
def test_aliases_resolve_to_their_preset(alias, preset):
    assert (rects(layout.resolve({'preset': alias}, 640, 360, ids(2))) ==
            rects(layout.resolve({'preset': preset}, 640, 360, ids(2))))


def test_solo_hides_the_others_rather_than_dropping_them():
    """A hidden source keeps its cell and its pad, so coming back is a property
    change rather than a reconnect."""
    cells = layout.resolve({'preset': 'solo', 'source': 's1'}, 1280, 720,
                           ids(3))
    assert cells['s1'].alpha == 1.0
    assert cells['s0'].alpha == 0.0
    assert cells['s2'].alpha == 0.0
    assert len(cells) == 3


def test_pip_insets_share_the_canvas_aspect_ratio():
    cells = layout.resolve({'preset': 'pip', 'source': 's0', 'size': 0.25},
                           1280, 720, ids(2))
    assert rects(cells)['s0'] == (0, 0, 1280, 720)
    inset = cells['s1']
    assert (inset.width, inset.height) == (320, 180)
    assert inset.zorder > cells['s0'].zorder


def test_pip_stacks_extra_insets_inward_from_the_corner():
    cells = layout.resolve({'preset': 'pip', 'source': 's0',
                            'corner': 'top-left', 'gap': 10},
                           1000, 1000, ids(3))
    assert cells['s1'].x == 10 and cells['s1'].y == 10
    assert cells['s2'].x == 10 + 250 + 10
    assert cells['s2'].y == 10


def test_spotlight_puts_the_rest_in_a_strip():
    cells = layout.resolve({'preset': 'spotlight', 'source': 's0',
                            'size': 0.25}, 1280, 720, ids(3))
    assert rects(cells)['s0'] == (0, 0, 1280, 540)
    assert cells['s1'].y == 540 and cells['s2'].y == 540
    assert cells['s1'].width + cells['s2'].width == 1280


def test_spotlight_with_nothing_to_put_in_the_strip_is_just_solo():
    cells = layout.resolve({'preset': 'spotlight', 'source': 's0'},
                           1280, 720, ids(1))
    assert rects(cells) == {'s0': (0, 0, 1280, 720)}


def test_spotlight_side_strip():
    cells = layout.resolve({'preset': 'spotlight', 'source': 's0',
                            'position': 'left', 'size': 0.2},
                           1000, 600, ids(3))
    assert rects(cells)['s0'] == (200, 0, 800, 600)
    assert cells['s1'].x == 0 and cells['s2'].x == 0
    assert cells['s1'].height + cells['s2'].height == 600


def test_subject_defaults_to_the_first_source():
    cells = layout.resolve({'preset': 'solo'}, 1280, 720, ids(2))
    assert cells['s0'].alpha == 1.0


# -- ordering --------------------------------------------------------------

def test_order_pins_the_leading_positions():
    cells = layout.resolve({'preset': 'row', 'order': ['s2', 's0']},
                           900, 100, ids(3))
    assert [k for k in cells] == ['s2', 's0', 's1']
    assert cells['s2'].x == 0 and cells['s0'].x == 300


def test_order_may_name_a_source_that_has_not_connected_yet():
    cells = layout.resolve({'preset': 'row', 'order': ['late', 's0']},
                           800, 100, ids(2))
    assert [k for k in cells] == ['s0', 's1']


def test_exclude_leaves_a_source_out_of_the_layout_entirely():
    cells = layout.resolve({'preset': 'grid', 'exclude': ['s1']},
                           1280, 720, ids(3))
    assert set(cells) == {'s0', 's2'}


def test_a_layout_with_no_sources_resolves_to_nothing():
    assert layout.resolve({'preset': 'grid'}, 1280, 720, []) == {}


# -- explicit cells --------------------------------------------------------

def test_explicit_cells_are_placed_verbatim():
    cells = layout.resolve({'cells': [
        {'source': 'a', 'x': 0, 'y': 0, 'width': 640, 'height': 720},
        {'source': 'b', 'x': 640, 'y': 180, 'width': 640, 'height': 360,
         'z': 5, 'fit': 'fill', 'alpha': 0.5},
    ]}, 1280, 720, ['a', 'b'])
    assert rects(cells) == {'a': (0, 0, 640, 720), 'b': (640, 180, 640, 360)}
    assert cells['b'].zorder == 5
    assert cells['b'].fit == 'fill'
    assert cells['b'].alpha == 0.5


def test_explicit_cells_leave_unnamed_sources_alone():
    """The difference between cells and a preset: cells never touch a source
    they do not name, so a hand-placed overlay survives one."""
    cells = layout.resolve({'cells': [
        {'source': 'a', 'x': 0, 'y': 0, 'width': 100, 'height': 100},
    ]}, 1280, 720, ['a', 'b'])
    assert set(cells) == {'a'}


def test_a_cell_for_a_source_that_has_not_connected_yet_is_held_open():
    """An operator can describe the whole show up front and have each camera
    land in its slot as it comes up."""
    spec = {'cells': [
        {'source': 'cam1', 'x': 0, 'y': 0, 'width': 640, 'height': 720},
        {'source': 'cam2', 'x': 640, 'y': 0, 'width': 640, 'height': 720},
    ]}
    assert set(layout.resolve(spec, 1280, 720, ['cam1'])) == {'cam1'}
    assert set(layout.resolve(spec, 1280, 720, ['cam1', 'cam2'])) == \
        {'cam1', 'cam2'}


def test_fractional_units_survive_a_change_of_resolution():
    spec = {'units': 'fraction', 'cells': [
        {'source': 'a', 'x': 0, 'y': 0, 'width': 0.5, 'height': 1.0},
        {'source': 'a2', 'x': 0.5, 'y': 0.25, 'width': 0.5, 'height': 0.5},
    ]}
    hd = layout.resolve(spec, 1280, 720, ['a', 'a2'])
    uhd = layout.resolve(spec, 3840, 2160, ['a', 'a2'])
    assert rects(hd) == {'a': (0, 0, 640, 720), 'a2': (640, 180, 640, 360)}
    assert rects(uhd) == {'a': (0, 0, 1920, 2160),
                          'a2': (1920, 540, 1920, 1080)}


def test_margin_applies_to_explicit_cells_too():
    cells = layout.resolve({'margin': 10, 'cells': [
        {'source': 'a', 'x': 0, 'y': 0, 'width': 100, 'height': 100},
    ]}, 1280, 720, ['a'])
    assert rects(cells) == {'a': (10, 10, 100, 100)}


# -- fit -------------------------------------------------------------------

def test_fit_defaults_to_contain_and_can_be_set_per_layout():
    assert layout.resolve({'preset': 'grid'}, 640, 360,
                          ['a'])['a'].fit == layout.CONTAIN
    assert layout.resolve({'preset': 'grid', 'fit': 'fill'}, 640, 360,
                          ['a'])['a'].fit == layout.FILL


def test_a_cell_fit_overrides_the_layout_fit():
    cells = layout.resolve({'fit': 'fill', 'cells': [
        {'source': 'a', 'x': 0, 'y': 0, 'width': 10, 'height': 10},
        {'source': 'b', 'x': 0, 'y': 0, 'width': 10, 'height': 10,
         'fit': 'contain'},
    ]}, 640, 360, ['a', 'b'])
    assert cells['a'].fit == layout.FILL
    assert cells['b'].fit == layout.CONTAIN


# -- rejected specs --------------------------------------------------------

@pytest.mark.parametrize('spec,expected', [
    ({}, 'either "preset" or "cells"'),
    ({'preset': 'grid', 'cells': []}, 'not both'),
    ({'preset': 'mosaic'}, 'unknown preset'),
    ({'preset': 'grid', 'colums': 2}, 'does not take "colums"'),
    ({'preset': 'grid', 'cols': 0}, 'positive whole number'),
    ({'preset': 'grid', 'cols': 1.5}, 'positive whole number'),
    ({'preset': 'grid', 'gap': -1}, 'cannot be negative'),
    ({'preset': 'grid', 'fit': 'cover'}, '"fit" must be one of'),
    ({'preset': 'solo', 'source': 'nobody'}, 'not a source of this stream'),
    ({'preset': 'grid', 'order': 'a'}, None),
    ({'preset': 'grid', 'order': [1]}, 'list of source ids'),
    ({'cells': {}}, 'must be a list'),
    ({'cells': [{'x': 0, 'y': 0, 'width': 1, 'height': 1}]}, 'needs a "source"'),
    ({'cells': [{'source': 'a', 'width': 1}]}, 'needs a "height"'),
    ({'cells': [{'source': 'a', 'width': 0, 'height': 10}]}, 'positive size'),
    ({'cells': [{'source': 'a', 'width': 10, 'height': 10, 'z': -1}]},
     'non-negative integer'),
    ({'cells': [{'source': 'a', 'width': 10, 'height': 10, 'top': 4}]},
     'does not take "top"'),
    ({'cells': [{'source': 'a', 'width': 10, 'height': 10},
                {'source': 'a', 'width': 10, 'height': 10}]}, 'twice'),
    ({'units': 'fraction',
      'cells': [{'source': 'a', 'width': 900, 'height': 0.5}]}, 'fraction'),
    ({'cells': [{'source': 'a', 'width': 0.5, 'height': 0.5}]},
     'whole number of pixels'),
    ({'units': 'percent', 'cells': []}, '"units" must be one of'),
    ('grid', 'must be a JSON object'),
])
def test_a_bad_spec_is_rejected(spec, expected):
    if expected is None:  # a string where a list belongs is a usable shorthand
        layout.resolve(spec, 1280, 720, ['a'])
        return
    with pytest.raises(layout.LayoutError) as excinfo:
        layout.resolve(spec, 1280, 720, ['a'])
    assert expected in str(excinfo.value)


def test_a_canvas_too_small_for_the_layout_is_rejected():
    with pytest.raises(layout.LayoutError) as excinfo:
        layout.resolve({'preset': 'grid', 'cols': 10, 'gap': 50},
                       320, 180, ids(10))
    assert 'cannot fit' in str(excinfo.value)


def test_a_margin_that_swallows_the_canvas_is_rejected():
    with pytest.raises(layout.LayoutError) as excinfo:
        layout.resolve({'preset': 'grid', 'margin': 200}, 320, 180, ids(1))
    assert 'leaves nothing' in str(excinfo.value)


def test_spotlight_that_leaves_no_room_for_the_subject_is_rejected():
    with pytest.raises(layout.LayoutError) as excinfo:
        layout.resolve({'preset': 'spotlight', 'source': 's0', 'size': 1.0},
                       1280, 720, ids(2))
    assert 'no room' in str(excinfo.value)
