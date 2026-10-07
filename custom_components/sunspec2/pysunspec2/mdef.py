import json
import os

'''
1. JSON is used for the native encoding of information model definitions.
2. JSON can be used to represent the values associated with information model points at a specific point in time.

Python support for information models:
- Python model support is based on dictionaries and their afinity with JSON objects.

Model instance notes:

- If a model contains repeating groups, the group counts must be known to fully initialized the model. If fields are
  accessed that depend on group counts that have not been initialized, a ModelError exception is generated.
- Points that have not been read or written contain a value of None.
- If a point that can not be changed from the initialized value is changed, a ModelError exception is generated.

A model definition is represented as a dictionary using the constants defined in this file as the entry keys.

A model definition is required to have a single top level group.

A model dict
  - must contain: 'id' and 'group'

A group dict
  - must contain: 'name', 'type', 'points'
  - may contain: 'count', 'groups', 'label', 'description', 'notes', 'comments'

A point dict
  - must contain: 'name', 'type'
  - may contain: 'count', 'size', 'sf', 'units', 'mandatory', 'access', 'symbols', 'label', 'description', 'notes',
                 'comments', 'standards'

A symbol dict
  - must contain: 'name', 'value'
  - may contain: 'label', 'description', 'notes', 'comments'

Example:
  model_def = {
    'id': 123,
    'group': {
      'id': model_name,
      'groups': [],
      'points': []
  }
'''

GROUP = 'group'             # top level model group (group dict)
GROUPS = 'groups'           # groups in group (list of group dicts)
POINTS = 'points'           # points in group (list of point dicts)

NAME = 'name'               # name (str)
COUNT = 'count'             # instance count (int or str)

TYPE = 'type'               # point type (str of TYPE_XXX)
SF = 'sf'                   # point scale factor (int)
SIZE = 'size'               # point string length (int)



TYPE_INT16 = 'int16'
TYPE_UINT16 = 'uint16'
TYPE_COUNT = 'count'
TYPE_ACC16 = 'acc16'
TYPE_ENUM16 = 'enum16'
TYPE_BITFIELD16 = 'bitfield16'
TYPE_PAD = 'pad'
TYPE_INT32 = 'int32'
TYPE_UINT32 = 'uint32'
TYPE_ACC32 = 'acc32'
TYPE_ENUM32 = 'enum32'
TYPE_BITFIELD32 = 'bitfield32'
TYPE_IPADDR = 'ipaddr'
TYPE_INT64 = 'int64'
TYPE_UINT64 = 'uint64'
TYPE_ACC64 = 'acc64'
TYPE_IPV6ADDR = 'ipv6addr'
TYPE_FLOAT32 = 'float32'
TYPE_FLOAT64 = 'float64'
TYPE_STRING = 'string'
TYPE_SUNSSF = 'sunssf'
TYPE_EUI48 = 'eui48'






MODEL_DEF_EXT = '.json'


def to_int(x):
    try:
        return int(x, 0)
    except TypeError:
        return int(x)


def to_str(s):
    return str(s)


def to_float(f):
    try:
        return float(f)
    except ValueError:
        return None







point_type_info = {
    TYPE_INT16: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_UINT16: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_COUNT: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_ACC16: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_ENUM16: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_BITFIELD16: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_PAD: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_INT32: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_UINT32: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_ACC32: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_ENUM32: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_BITFIELD32: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_IPADDR: {'len': 2, 'to_type': to_int, 'default': 0},
    TYPE_INT64: {'len': 4, 'to_type': to_int, 'default': 0},
    TYPE_UINT64: {'len': 4, 'to_type': to_int, 'default': 0},
    TYPE_ACC64: {'len': 4, 'to_type': to_int, 'default': 0},
    TYPE_IPV6ADDR: {'len': 8, 'to_type': to_str, 'default': 0},
    TYPE_FLOAT32: {'len': 2, 'to_type': to_float, 'default': 0},
    TYPE_FLOAT64: {'len': 4, 'to_type': to_float, 'default': 0},
    TYPE_STRING: {'len': None, 'to_type': to_str, 'default': ''},
    TYPE_SUNSSF: {'len': 1, 'to_type': to_int, 'default': 0},
    TYPE_EUI48: {'len': 4, 'to_type': to_str, 'default': 0}
}


class ModelDefinitionError(Exception):
    pass
























def from_json_file(filename):
    f = open(filename)
    model_def = json.load(f)
    f.close()
    return model_def




def to_json_filename(model_id):
    return 'model_%s%s' % (model_id, MODEL_DEF_EXT)






def get_group_len_points(group_def, points=None):
    if points is None:
        points = []
    groups = group_def.get(GROUPS)
    if groups:
        for g in groups:
            count = g.get(COUNT)
            if count:
                try:
                    count = int(count)
                except:
                    if count not in points:
                        points.append(count)
            points = get_group_len_points(g, points)
    return points


def get_group_len_points_index(group_def):
    index = pindex = 0
    if group_def:
        len_points = get_group_len_points(group_def)
        if len_points:
            points = group_def.get(POINTS, [])
            for p in points:
                name = p.get(NAME)
                plen = point_type_info.get(p.get(TYPE)).get('len')
                if plen is None:
                    plen = p.get(SIZE)  # use 'size' from sunspec model for string points
                if not plen:
                    raise ModelDefinitionError('Unable to get size of point %s' % name)
                pindex += plen
                if name in len_points:
                    index = pindex
                    len_points.remove(name)
                if not len_points:
                    break
            if len_points:
                raise ModelDefinitionError('Expected points not found in group definition: %s' % len_points)
    return index


if __name__ == "__main__":
    pass
