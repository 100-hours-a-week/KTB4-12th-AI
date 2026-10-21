"""Reuse the existing mandatory-execution/store guards for remote Search."""


class SharedSearchBinding:
    def __init__(self, catalog):
        self.catalog = catalog
        self.check()

    @property
    def version(self):
        return str(self.catalog.catalog_version)

    @property
    def count(self):
        return self.catalog.total

    def check(self):
        return self.catalog.check()
