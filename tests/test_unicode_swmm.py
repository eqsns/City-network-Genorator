import bootstrap
import numpy as np
from pathlib import Path
import pytest
from swmmanywhere.post_processing import overwrite_section,change_flow_routing


def test_inp_uses_utf8_with_unicode_paths(tmp_path):
    file=tmp_path/'中文 空格.inp'
    file.write_text('[OPTIONS]\nFLOW_ROUTING DYNWAVE\n[RAINGAGES]\n;;name type interval factor format path station unit\n1 INTENSITY 00:05 1 FILE test.dat 1 mm\n\n[END]\n',encoding='utf-8')
    data=np.array([['1','INTENSITY','00:05','1','FILE',r'C:\软件搬迁 空格\storm.dat','1','mm']])
    overwrite_section(data,'[RAINGAGES]',file)
    change_flow_routing('DYNWAVE',file)
    assert '软件搬迁 空格' in file.read_text(encoding='utf-8')


@pytest.mark.parametrize('bom', ['', '\ufeff'])
def test_config_reads_unicode_paths_with_windows_legacy_default(tmp_path, monkeypatch, bom):
    from swmmanywhere.swmmanywhere import load_config
    config = tmp_path/'中文 配置.yml'
    config.write_text(bom+'base_dir: C:/软件 搬迁/成果\nproject: 延安\n', encoding='utf-8')
    original = Path.read_text
    def legacy_read(self, encoding=None, errors=None):
        return original(self, encoding=encoding or 'gbk', errors=errors)
    monkeypatch.setattr(Path, 'read_text', legacy_read)
    result = load_config(config, validation=False)
    assert str(result['base_dir']) == 'C:/软件 搬迁/成果'
    assert result['project'] == '延安'
