import bootstrap
import numpy as np
import pytest
from swmmanywhere.parameters import HydraulicDesign
from swmmanywhere.graphfcns.design_graphfcns import pipe_by_pipe


def test_design_cost_is_invariant_to_vertical_datum():
    designer=pipe_by_pipe()
    params=HydraulicDesign()
    # Identical buried pipe, expressed using two different elevation datums.
    high=designer.evaluate_design(10,9,100,params,.01,.3,2,upstream_depth=2)
    low=designer.evaluate_design(-10,-11,100,params,.01,.3,2,upstream_depth=2)
    assert high==pytest.approx(low)
    assert np.isfinite(low['cost_usd']) and low['cost_usd']>0


def test_low_elevation_still_has_valid_pipe_design():
    result=pipe_by_pipe().design_pipe(-5,-6,100,HydraulicDesign(),.01,upstream_depth=1)
    assert result['diameter']>0
    assert all(np.isfinite(v) for v in result.values())


def test_excavation_cost_increases_with_burial_depth():
    designer=pipe_by_pipe()
    params=HydraulicDesign()
    shallow=designer.evaluate_design(0,-1,100,params,.01,.3,2,upstream_depth=1)
    deep=designer.evaluate_design(0,-1,100,params,.01,.3,2,upstream_depth=4)
    assert deep['cost_usd']>shallow['cost_usd']
