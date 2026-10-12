"""Collect bundled dependency licenses and reproducibility metadata."""
from pathlib import Path
import importlib.metadata as metadata
import shutil
import json
import zipfile
root=Path(__file__).resolve().parents[1]
target=root/'licenses'
target.mkdir(exist_ok=True)
records=[]
for dist in metadata.distributions():
    name=dist.metadata['Name']
    records.append({'name':name,'version':dist.version,'license':dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See license files'})
    for file in dist.files or []:
        if any(word in file.name.lower() for word in ['license','copying','notice']):
            source=Path(dist.locate_file(file))
            if source.is_file():
                dest=target/name/str(file).replace('../','').replace('..\\','')
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,dest)
shutil.copy2(root/'vendor'/'LICENSE-SWMManywhere',target/'LICENSE-SWMManywhere.txt')
shutil.copy2(root/'LICENSE',target/'LICENSE-CNG.txt')
shutil.copy2(root/'vendor'/'swmmanywhere-src'/'netcomp'/'LICENSE.txt',target/'LICENSE-netcomp.txt')
for file in (root/'web'/'vendor').glob('LICENSE-*'):
    shutil.copy2(file,target/(file.name+'.txt'))
with zipfile.ZipFile(root/'native'/'whitebox_tools.zip') as archive:
    for item in archive.infolist():
        if 'license' in item.filename.lower() and not item.is_dir():
            (target/('WhiteboxTools-'+Path(item.filename).name)).write_bytes(archive.read(item))
(target/'dependency-manifest.json').write_text(json.dumps(records,indent=2,ensure_ascii=False),encoding='utf-8')
shutil.copy2(root/'requirements-lock.txt',target/'requirements-lock.txt')
