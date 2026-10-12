import pytest
from artifacts import portable_rain_path


@pytest.mark.parametrize('source', [r'C:\Users\test\storm.dat',r'C:\中文 空格\storm.dat',r'"C:\中文 空格\storm.dat"','"storm.dat"'])
def test_portable_rain_file(source):
    text='[RAINGAGES]\n1 INTENSITY 00:05 1 FILE '+source+' 1 mm\n[SYMBOLS]\n'
    assert portable_rain_path(text)=='[RAINGAGES]\n1 INTENSITY 00:05 1 FILE "storm.dat" 1 mm\n[SYMBOLS]\n'
