"""Create a checksum-verified portable ZIP from the clean staged build."""
from pathlib import Path
import hashlib,json,zipfile
root=Path(__file__).resolve().parents[1]
stage=root/'.cache'/'release-stage'/'SWMManywhereStudio'
output=root/'dist'/'SWMManywhereStudio-Windows-x64.zip'
temporary=output.with_suffix('.zip.tmp')
with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as archive:
    for file in sorted(stage.rglob('*')):
        if file.is_file():
            archive.write(file,Path('SWMManywhereStudio')/file.relative_to(stage))
with zipfile.ZipFile(temporary) as archive:
    failure=archive.testzip()
    if failure:raise RuntimeError(failure)
with temporary.open('rb') as stream:
    digest=hashlib.file_digest(stream,'sha256').hexdigest()
temporary.replace(output)
(output.with_suffix('.zip.sha256')).write_text(f'{digest}  {output.name}\n',encoding='ascii')
print(json.dumps({'zip':str(output),'bytes':output.stat().st_size,'sha256':digest}),flush=True)
