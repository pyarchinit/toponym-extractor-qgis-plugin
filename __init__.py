def classFactory(iface):
    from .toponimi_extractor import ToponimiExtractorPlugin
    return ToponimiExtractorPlugin(iface)
