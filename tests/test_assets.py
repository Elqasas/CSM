"""The stylesheet URL changes when the file does, on the dev server only."""

from __future__ import annotations

from csm.core.assets import asset_version


def test_debug_versions_the_stylesheet_by_its_modification_time(settings):
    settings.DEBUG = True
    assert asset_version(None)["asset_version"].isdigit()


def test_production_leaves_it_to_the_manifest_hash(settings):
    settings.DEBUG = False
    assert asset_version(None) == {"asset_version": ""}
