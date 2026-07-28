from .session_quality import SessionQualityAnalysis


_MODULES = {
    module.name: module
    for module in (SessionQualityAnalysis(),)
}


def modules():
    return list(_MODULES.values())


def get_module(name, version=None):
    module = _MODULES.get(name)
    if module is None or (version is not None and module.version != version):
        return None
    return module

