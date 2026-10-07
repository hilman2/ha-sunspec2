import custom_components.sunspec2.pysunspec2.mdef as mdef
import json
import copy
import pytest


def test_to_int():
    assert mdef.to_int('4') == 4
    assert isinstance(mdef.to_int('4'), int)
    assert isinstance(mdef.to_int(4.0), int)


def test_to_str():
    assert mdef.to_str(4) == '4'
    assert isinstance(mdef.to_str('4'), str)


def test_to_float():
    assert mdef.to_float('4') == 4.0
    assert isinstance(mdef.to_float('4'), float)
    assert mdef.to_float('z') is None
























def test_from_json_file():
    assert isinstance(mdef.from_json_file('./custom_components/sunspec2/pysunspec2/models/json/model_63001.json'), dict)




def test_to_json_filename():
    assert mdef.to_json_filename('63001') == 'model_63001.json'




