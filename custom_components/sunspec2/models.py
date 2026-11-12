"""SunSpec model wrapper.

A thin facade over a list of pysunspec2 model instances. It gives the
points a flat ``key`` namespace (repeating-group points such as
``module:0:DCA`` included), filters out points without a value or a
unit, and returns the scaled and decoded value through
:meth:`SunSpecModelWrapper.getValue`. The wrapper sees only pysunspec2
model objects and depends on nothing else in the integration.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

#: The raw word SunSpec reserves for "not implemented" in an int16.
SIGNED_NAN_WORD = 0x8000


class SunSpecModelWrapper:
    """Wraps the list of pysunspec2 model instances for a single model id.

    A SunSpec device may expose the same model id several times. The
    wrapper holds every instance and gives their number in ``num_models``.
    """

    def __init__(
        self,
        models: list[Any],
        point_transforms: Mapping[str, Callable[[Any], Any]] | None = None,
        signed_nan_points: frozenset[str] = frozenset(),
    ) -> None:
        """Wrap every instance a device exposes for one model id.

        Args:
            models: The pysunspec2 model instances, in the order the
                device reports them. Untyped because pysunspec2 ships no
                py.typed marker.
            point_transforms: Read corrections applied after SunSpec scaling,
                keyed by point name. The underlying points stay unchanged.
            signed_nan_points: Unsigned points whose raw 0x8000 means no
                reading, by point name. See ``VendorProfile.signed_nan_points``.
        """
        self._models = models
        self.num_models = len(models)
        self._point_transforms = point_transforms or {}
        self._signed_nan_points = signed_nan_points

    def isValidPoint(self, point_name: str) -> bool:
        point = self.getPoint(point_name)
        if point.value is None:
            return False
        if point.pdef["type"] in ("enum16", "bitfield32"):
            return True
        return point.pdef.get("units", None) is not None

    def getKeys(self) -> list[str]:
        keys = list(filter(self.isValidPoint, self._models[0].points.keys()))
        for group_name in self._models[0].groups:
            model_group = self._models[0].groups[group_name]
            if type(model_group) is list:
                for idx, group in enumerate(model_group):
                    key_prefix = f"{group_name}:{idx}"
                    group_keys = map(lambda gp: f"{key_prefix}:{gp}", group.points.keys())
                    keys.extend(filter(self.isValidPoint, group_keys))
            else:
                key_prefix = f"{group_name}:0"
                group_keys = map(lambda gp: f"{key_prefix}:{gp}", model_group.points.keys())
                keys.extend(filter(self.isValidPoint, group_keys))
        return keys

    def getValue(self, point_name: str, model_index: int = 0) -> Any:
        point = self.getPoint(point_name, model_index)
        if point_name in self._signed_nan_points and point.value == SIGNED_NAN_WORD:
            return None
        transform = self._point_transforms.get(point_name)
        if transform is not None:
            return transform(point.cvalue)
        return point.cvalue

    def getMeta(self, point_name: str) -> dict[str, Any]:
        # The annotation is the assertion: pysunspec2 hands back an
        # untyped pdef, and this is where that Any stops travelling.
        pdef: dict[str, Any] = self.getPoint(point_name).pdef
        return pdef

    def getGroupMeta(self) -> dict[str, Any]:
        gdef: dict[str, Any] = self._models[0].gdef
        return gdef

    def getPoint(self, point_name: str, model_index: int = 0) -> Any:
        point_path = point_name.split(":")
        if len(point_path) == 1:
            return self._models[model_index].points[point_name]

        group = self._models[model_index].groups[point_path[0]]
        if type(group) is list:
            return group[int(point_path[1])].points[point_path[2]]
        return group.points[point_path[2]]


def point_text(wrapper: SunSpecModelWrapper, point_name: str) -> str:
    """The text a string point holds, or ``""`` when there is none.

    Common model 1 identifies the device in string points (``Mn``,
    ``Md``, ``Opt``, ``Vr``), and the vendor profile is picked from
    them. A caller that compares them gets a string every time: ``""``
    when the model lacks the point or its value is not a string.

    Args:
        wrapper: The model to read from.
        point_name: The point, by its SunSpec name.
    """
    try:
        value = wrapper.getValue(point_name)
    except (KeyError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""
