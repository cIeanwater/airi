"""Download pinned inference files from repositories linked by the official project."""

import concurrent.futures
import argparse
import hashlib
import json
import subprocess
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-dir', type=Path, default=Path(__file__).resolve().parents[2] / '.tools/gpt-sovits-v2proplus')
args = parser.parse_args()
ROOT = args.runtime_dir.resolve()
SOURCE_REVISION = '1b47ca07ef43a284e0b8bd7f29d3e1e3ff76082a'
if not ROOT.exists():
    subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout', 'https://github.com/RVC-Boss/GPT-SoVITS.git', str(ROOT)], check=True)
    subprocess.run(['git', '-C', str(ROOT), 'checkout', '--detach', SOURCE_REVISION], check=True)
revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
if revision != SOURCE_REVISION:
    raise ValueError('The existing runtime has another source revision. Use a new runtime directory')
MODELS = ROOT / 'GPT_SoVITS' / 'pretrained_models'
REPOSITORIES = [
    ('lj1995/GPT-SoVITS', '8f8857563c040d542de108a2c18b0425abd6c9c5', (
        's1v3.ckpt', 'v2Pro/s2Gv2ProPlus.pth', 'sv/pretrained_eres2netv2w24s4ep4.ckpt',
        'chinese-roberta-wwm-ext-large/', 'chinese-hubert-base/',
    )),
    ('XXXXRT/GPT-SoVITS-Pretrained', '0c47645e02a7bc3688d7b263b0042c81e3cd82cd', ('G2PWModel.zip',)),
]


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def valid(path, entry):
    if not path.is_file() or path.stat().st_size != entry['size']:
        return False
    digest = hashlib.sha256() if entry.get('lfs') else hashlib.sha1()
    if not entry.get('lfs'):
        digest.update(f"blob {entry['size']}\0".encode('ascii'))
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    expected = entry['lfs']['sha256'] if entry.get('lfs') else entry['blobId']
    return digest.hexdigest() == expected


def download(item):
    repo, revision, entry = item
    relative = entry['rfilename']
    base = ROOT / 'downloads' if relative.endswith('.zip') else MODELS
    target = (base / relative).resolve()
    if ROOT not in target.parents:
        raise ValueError(f'Unsafe download path: {relative}: {target}: {ROOT}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not valid(target, entry):
        temporary = target.with_suffix(target.suffix + '.download')
        url = entry.get('download_url') or f"https://huggingface.co/{repo}/resolve/{revision}/{urllib.parse.quote(relative, safe='/')}"
        print(json.dumps({'download': relative, 'bytes': entry['size']}), flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                output.write(chunk)
        if not valid(temporary, entry):
            raise ValueError(f'Publisher hash mismatch: {relative}')
        temporary.replace(target)
    print(json.dumps({'verified': relative, 'bytes': target.stat().st_size}), flush=True)
    return {'repo': repo, 'revision': revision, **entry}


items = []
for repo, revision, prefixes in REPOSITORIES:
    info = get_json(f'https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true')
    for entry in info['siblings']:
        if entry['rfilename'].startswith(prefixes):
            items.append((repo, revision, entry))

items.append(('facebook/fasttext', 'lid.176', {
    'rfilename': 'fast_langdetect/lid.176.bin', 'size': 131266198,
    'lfs': {'sha256': '7e69ec5451bc261cc7844e49e4792a85d7f09c06789ec800fc4a44aec362764e'},
    'download_url': 'https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin',
}))

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    verified = list(executor.map(download, items))

destination = (ROOT / 'GPT_SoVITS/text/G2PWModel').resolve()
with zipfile.ZipFile(ROOT / 'downloads/G2PWModel.zip') as archive:
    names = [member for member in archive.infolist() if not member.is_dir()]
    prefixes = {Path(member.filename).parts[0] for member in names}
    for member in names:
        parts = Path(member.filename).parts
        relative = Path(*parts[1:]) if len(prefixes) == 1 and len(parts) > 1 else Path(*parts)
        target = (destination / relative).resolve()
        if destination not in target.parents:
            raise ValueError('Unsafe model archive path')
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open('wb') as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                output.write(chunk)

patches = []
for filename in ('chinese.py', 'chinese2.py', 'tone_sandhi.py'):
    relative = 'GPT_SoVITS/text/' + filename
    upstream = subprocess.check_output(['git', '-C', str(ROOT), 'show', SOURCE_REVISION + ':' + relative]).decode('utf-8')
    # NOTICE:
    # jieba_fast requires an unavailable C++ compiler on this Windows machine.
    # The official project also declares the original jieba dependency.
    # Use its matching public API, without an import shim or edits to installed packages.
    # Remove this patch when the pinned jieba_fast release provides a supported Windows wheel.
    patched = upstream.replace('jieba_fast', 'jieba')
    target = ROOT / relative
    current = target.read_text(encoding='utf-8')
    if current not in (upstream, patched):
        raise ValueError('The runtime source contains unrelated changes: ' + relative)
    target.write_text(patched, encoding='utf-8', newline='')
    patches.append({'path': relative, 'sha256': hashlib.sha256(patched.encode()).hexdigest(), 'change': 'jieba_fast to jieba'})

manifest = {'source_repo':'https://github.com/RVC-Boss/GPT-SoVITS', 'source_revision': SOURCE_REVISION, 'source_patches': patches, 'files':verified}
(ROOT / 'publisher-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'ready':True,'verified_files':len(verified),'total_download_bytes':sum(item['size'] for item in verified)}), flush=True)
