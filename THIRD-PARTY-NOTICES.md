# Third-party components

CNG 城市排水管网生成 uses the user-provided SWMManywhere 0.2.2 source distribution.
This is an independent desktop adaptation, not an official SWMManywhere release.
Local changes include structured progress, cooperative execution control, bounded network recovery,
cache validation, geometry/compatibility fixes, desktop UI, history management and export packaging.
The upstream source and copyright notice are preserved in vendor/swmmanywhere-src and vendor/LICENSE-SWMManywhere.
SWMManywhere and netcomp license texts, and Python package notices, are included in `_internal/licenses`.

- SWMManywhere: https://github.com/ImperialCollegeLondon/SWMManywhere
- Leaflet 1.9.4: https://leafletjs.com/ (BSD-2-Clause)
- Leaflet.draw 1.0.4: https://github.com/Leaflet/Leaflet.draw (MIT)
- pywebview: https://pywebview.flowrl.com/ (BSD-3-Clause)
- OpenStreetMap data and map tiles: https://www.openstreetmap.org/copyright
- Tile service policy: https://operations.osmfoundation.org/policies/tiles/
- OSM data service fallback: https://overpass.private.coffee/api/ (only after primary transport failure); source listing: https://wiki.openstreetmap.org/wiki/Overpass_API
- Overture buildings: https://docs.overturemaps.org/attribution/
- Elevation source: NASADEM through Microsoft Planetary Computer
- WhiteboxTools: distributed in the original upstream binary ZIP, with its bundled notices preserved.
  Apple Silicon build input: https://www.whiteboxgeo.com/WBT_Darwin/WhiteboxTools_darwin_m_series.zip
  SHA256: b0ff9ad48769df68a604c266e249eb4867ce7505fc297f439cf2cefe0c185c8b
  macOS uses the system WebKit through pywebview/PyObjC; the Mac build remains pending native validation.
- Microsoft WebView2 Fixed Runtime 154.0.4258.37 x64: redistributed separately from the open-source application code; Microsoft's terms apply.
  Official download: https://developer.microsoft.com/microsoft-edge/webview2/
  Distribution documentation: https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution
  Runtime source: https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/b82d47e8-d146-4563-94d1-3a3176b25c0a/Microsoft.WebView2.FixedVersionRuntime.154.0.4258.37.x64.cab

The fixed runtime is not automatically updated. Publish application updates with an updated runtime as necessary.
