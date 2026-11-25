#!/usr/bin/env python3
"""短期予報解説資料 → 日英学習ページ 自動生成パイプライン

使い方:
  python build/pipeline.py fetch   # 最新PDFを取得し、新しい号なら英訳＋音声を作る
  python build/pipeline.py render  # data/ と site/ から全ページを作り直し、古い音声を消す

フォルダ:
  data/<ID>.json        号ごとの本文・英訳・単語・音声タイミング（main ブランチに保存）
  site/                 公開用（gh-pages ブランチ。履歴を持たない）
    issues/<ID>/        index.html, chart.jpg, audio.mp3, vocab.mp3
    index.html          最新号へ移動
    archive.html        バックナンバー一覧
"""
import base64, datetime as dt, html, json, os, re, shutil, subprocess, sys, unicodedata, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
SITE = ROOT / 'site'
TPL = ROOT / 'build' / 'issue_tpl.html'
PDF_URL = 'https://www.data.jma.go.jp/yoho/data/jishin/kaisetsu_tanki_latest.pdf'
MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
AUDIO_DAYS = int(os.environ.get('AUDIO_DAYS', '31'))
JST = dt.timezone(dt.timedelta(hours=9))
WORK = ROOT / '.work'

LEGEND = [['実況', '解析時点の状況（analysis）'],
          ['FT12〜FT48', '12〜48時間先の予想（forecast time）'],
          ['kt', 'ノット（knots）。風速の単位']]


def log(*a):
    print('[tanki]', *a, flush=True)


# ---------- fetch ----------
def download_pdf():
    WORK.mkdir(exist_ok=True)
    p = WORK / 'latest.pdf'
    req = urllib.request.Request(PDF_URL, headers={'User-Agent': 'tanki-study (github.com/tenkibeat/tanki)'})
    with urllib.request.urlopen(req, timeout=60) as r:
        p.write_bytes(r.read())
    return p


def pdf_text(pdf):
    return subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True, check=True).stdout


def issue_id(text):
    t = unicodedata.normalize('NFKC', text)
    m = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2})時\s*(\d{1,2})分発表', t)
    if not m:
        raise SystemExit('発表日時が読めませんでした')
    y, mo, d, h, mi = map(int, m.groups())
    no = re.search(r'短期予報解説資料\s*(\d+)', t)
    return f'{y:04d}{mo:02d}{d:02d}-{h:02d}{mi:02d}', dt.datetime(y, mo, d, h, mi, tzinfo=JST), (no.group(1) if no else '1')


def extract_chart(pdf, dest):
    tmp = WORK / 'img'
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    subprocess.run(['pdfimages', '-j', '-png', str(pdf), str(tmp / 'im')], check=False)
    imgs = sorted(tmp.glob('im-*'), key=lambda f: f.stat().st_size, reverse=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if imgs and imgs[0].stat().st_size > 20000:
        src = imgs[0]
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src), '-vf', "scale='min(1600,iw)':-2", '-q:v', '4', str(dest)], check=True)
        return 'image'
    # 画像が取り出せないときはページ全体を画像にする
    subprocess.run(['pdftoppm', '-jpeg', '-r', '150', '-singlefile', str(pdf), str(dest.with_suffix(''))], check=True)
    return 'page'


SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'title_en': {'type': 'STRING'},
        'sections': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'heading': {'type': 'BOOLEAN'}, 'ja': {'type': 'STRING'},
            'en': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
            'ja_parts': {'type': 'ARRAY', 'items': {'type': 'STRING'}}}, 'required': ['heading', 'ja', 'en', 'ja_parts']}},
        'vocab': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'w': {'type': 'STRING'}, 'pos': {'type': 'STRING'}, 'ipa': {'type': 'STRING'}, 'ja': {'type': 'STRING'},
            'ex': {'type': 'STRING'}, 'exja': {'type': 'STRING'}}, 'required': ['w', 'pos', 'ipa', 'ja', 'ex', 'exja']}},
        'chart_notes': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'k': {'type': 'STRING'}, 'v': {'type': 'STRING'}}, 'required': ['k', 'v']}},
    },
    'required': ['title_en', 'sections', 'vocab', 'chart_notes'],
}

PROMPT = """あなたは気象庁の予報解説を英語に訳す、気象予報士向け英語教材の編集者です。
添付PDFは気象庁「短期予報解説資料」です。参考として抽出テキストも付けます（改行やスペースは崩れています）。

次のJSONを作ってください。
1. sections: 本文を上から順に区切る。
   - 見出し（「１．実況上の着目点」など、全角数字＋「．」で始まる行）は heading=true。
   - 本文は ①②… の項目ごと、または見出し直下の段落ごとに1要素（heading=false）。
   - ja_parts は、en の各文に対応する日本語の部分を、en と同じ数・同じ順番で並べた配列。ja_parts をつなげると ja と1字も違わず同じになるように、ja を切り分ける（言い換え・省略・重複は禁止）。日本語1文が英語2文になったときは、日本語を読点などで切り分けてそろえる。うまく切り分けられないときは、関係の近い英文の側にまとめてよい（そのときも数はそろえる）。見出しは ja_parts=[ja]
   - 見出しと本文が同じ行に続いている場合（例「３．数値予報資料解釈上の留意点 総観場は…」）は、見出しと本文を別の要素に分ける。
   - 「４．防災関連事項」の①②③は1つの本文要素にまとめてよい。
   - 最後の欄外注記（「量的な予報については…」）も本文要素として入れ、ja の先頭に「（注）」を付ける。
   - ja は原文をそのまま。PDF上の改行・余分なスペースだけ取り除き、文字は一字も変えない（言い換え・要約・助詞の変更は禁止）。
   - en は ja の英訳を「1文ずつの配列」にする。見出しは1要素。項目番号 ① は (1) のように訳文の先頭に付ける。
   - 用語は気象の専門用語として正確に：トラフ=trough、強風軸=strong-wind axis (jet axis)、高度=geopotential height、暖気移流=warm advection、筋状雲=cloud streets、気圧の谷=trough of low pressure、移動性高気圧=migratory anticyclone、下層暖湿気=warm, moist air in the lower layers、正渦度=positive vorticity、大気の状態が不安定=the atmosphere becomes unstable。
   - 「注意」は "be alert to / watch for"、「警戒」は "be on high alert for" と訳し分ける。
   - 数値・単位・日付は原文どおり（500hPa → 500 hPa、5700～5820m → 5,700–5,820 m）。
2. title_en: 例 "JMA Short-Range Forecast Discussion No. 1 — issued 15:40 JST, 25 November 2026"
3. vocab: この号に出てくる重要な気象英語を25〜35語。w=英語の語句、pos=品詞（日本語。例：名詞、名詞句、動詞、形容詞）、ipa=米音のIPA（/ /で囲む）、ja=日本語の意味、ex=今回の英訳から取った短い例文（または例文の一部）、exja=その日本語。
4. chart_notes: 解説図の中に書かれている凡例・注記（例：破線＝地上のシアーライン、前線記号の黒塗り＝FT24）だけを [k=記号, v=意味] で。図に書かれていないことは書かない。なければ空配列。
"""


def gemini(pdf, text):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise SystemExit('GEMINI_API_KEY がありません')
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
    body = {
        'contents': [{'role': 'user', 'parts': [
            {'inline_data': {'mime_type': 'application/pdf', 'data': base64.b64encode(pdf.read_bytes()).decode()}},
            {'text': PROMPT + '\n\n--- 抽出テキスト ---\n' + text}]}],
        'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': SCHEMA, 'temperature': 0.2},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f'Gemini API エラー {e.code}: {e.read()[:800].decode(errors="replace")}')
    out = ''.join(p.get('text', '') for p in res['candidates'][0]['content']['parts'])
    return json.loads(out)


JP = r'[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]'


def tidy_ja(s):
    # PDF由来の「28 日」「500hPa 5700」などの余分な半角スペースを、日本語の文字に隣接するものだけ取り除く
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(rf'(?<={JP}) +| +(?={JP})', '', s)
    return re.sub(r'([\u2460-\u2473]) *', r'\1 ', s)


def nz(s):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', s))


def split_ja(ja, n):
    # 対応表がないときの簡易版：「。」で切って、英文の数とそろえば使う
    parts = [p for p in re.findall(r'[^。]*。|[^。]+$', ja) if p.strip()]
    return parts if len(parts) == n else None


def check_parts(s):
    parts = [tidy_ja(p) for p in s.get('ja_parts') or []]
    if len(parts) == len(s['en']) and nz(''.join(parts)) == nz(s['ja']) and all(p.strip() for p in parts):
        return parts
    log('対応表が合わないので簡易版を使います:', s['ja'][:20])
    return split_ja(s['ja'], len(s['en']))


def check_ja(result, text):
    norm = lambda s: re.sub(r'\s+', '', unicodedata.normalize('NFKC', s)).replace('（注）', '')
    src = norm(text)
    bad = [s['ja'][:30] for s in result['sections'] if norm(s['ja']) not in src]
    if bad:
        log('注意: 原文と一致しない日本語があります:', bad)
    return bad


def speech_text(s):
    s = s.replace('–', ' to ').replace('—', ', ')
    s = re.sub(r'(\d)\s*hPa', r'\1 hectopascals', s)
    s = re.sub(r'(\d)\s*m\b', r'\1 meters', s)
    s = re.sub(r'(\d)\s*mm\b', r'\1 millimeters', s)
    s = re.sub(r'(\d)\s*cm\b', r'\1 centimeters', s)
    return s


def make_audio(d, outdir):
    import numpy as np, soundfile as sf
    from kokoro_onnx import Kokoro
    mdir = Path(os.environ.get('KOKORO_DIR', ROOT / '.kokoro'))
    k = Kokoro(str(mdir / 'kokoro-v1.0.onnx'), str(mdir / 'voices-v1.0.bin'))
    sents = [s for x in d['sections'] for s in x['en']]
    ch, times, pos = [], [], 0.0
    for s in sents:
        a, sr = k.create(speech_text(s), voice='af_heart', speed=0.95, lang='en-us')
        times.append(round(pos, 2)); ch += [a, np.zeros(int(sr * 0.45), dtype=a.dtype)]; pos += len(a) / sr + 0.45
    wav = WORK / 'a.wav'; sf.write(wav, np.concatenate(ch), sr)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(outdir / 'audio.mp3')], check=True)
    ch, vt, pos = [], [], 0.0
    for v in d['vocab']:
        a, sr = k.create(speech_text(v['w']), voice='af_heart', speed=0.9, lang='en-us')
        b, sr = k.create(speech_text(v['ex']), voice='af_heart', speed=0.95, lang='en-us')
        clip = np.concatenate([a, np.zeros(int(sr * 0.5), dtype=a.dtype), b])
        vt.append([round(pos, 2), round(pos + len(clip) / sr, 2)])
        ch += [clip, np.zeros(int(sr * 0.3), dtype=a.dtype)]; pos += len(clip) / sr + 0.3
    wav = WORK / 'v.wav'; sf.write(wav, np.concatenate(ch), sr)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(outdir / 'vocab.mp3')], check=True)
    d['times'], d['vtimes'] = times, vt


def cmd_fetch():
    pdf = download_pdf()
    text = pdf_text(pdf)
    iid, when, no = issue_id(text)
    if (DATA / f'{iid}.json').exists():
        log('新しい号はありません:', iid)
        return False
    log('新しい号:', iid)
    outdir = SITE / 'issues' / iid
    outdir.mkdir(parents=True, exist_ok=True)
    extract_chart(pdf, outdir / 'chart.jpg')
    r = gemini(pdf, text)
    for s in r['sections']:
        s['ja'] = tidy_ja(s['ja'])
        s['ja_parts'] = check_parts(s)
    for v in r['vocab']:
        v['exja'] = tidy_ja(v['exja'])
    mism = check_ja(r, text)
    d = {'id': iid, 'issued': when.isoformat(), 'no': no, 'title_en': r['title_en'], 'by': f'Gemini（{MODEL}）',
         'sections': r['sections'], 'vocab': r['vocab'],
         'legend': LEGEND + [[n['k'], n['v']] for n in r.get('chart_notes', [])],
         'ja_mismatch': mism}
    make_audio(d, outdir)
    DATA.mkdir(exist_ok=True)
    (DATA / f'{iid}.json').write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    return True


# ---------- render ----------
def ja_date(when):
    return f'{when.month}/{when.day} {when:%H:%M}'


def render_issue(d):
    when = dt.datetime.fromisoformat(d['issued'])
    outdir = SITE / 'issues' / d['id']
    outdir.mkdir(parents=True, exist_ok=True)
    has_audio = (outdir / 'audio.mp3').exists() and (outdir / 'vocab.mp3').exists()
    pub = {k: d[k] for k in ('sections', 'vocab', 'legend')}
    pub['secs'] = [{'h': s['heading'], 'ja': s['ja'], 'en': s['en'],
                    'jp': s.get('ja_parts') or ([s['ja']] if s['heading'] else split_ja(s['ja'], len(s['en'])))}
                   for s in d['sections']]
    pub.update(times=d.get('times', []), vtimes=d.get('vtimes', []), audio=has_audio)
    del pub['sections']
    title_ja = f'短期予報解説資料{d.get("no","1")}　{when.year}年{when.month}月{when.day}日{when:%H}時{when:%M}分発表　気象庁'
    t = TPL.read_text(encoding='utf-8')
    rep = {'%%PTITLE%%': f'短期予報解説 {ja_date(when)} 英語', '%%H1%%': f'短期予報解説　{ja_date(when)}',
           '%%TEN%%': html.escape(d['title_en']), '%%TJA%%': html.escape(title_ja), '%%SRC%%': PDF_URL,
           '%%BY%%': html.escape(d.get('by', '')),
           '%%DATA%%': json.dumps(pub, ensure_ascii=False).replace('</', '<\\/')}
    for k, v in rep.items():
        t = t.replace(k, v)
    (outdir / 'index.html').write_text(t, encoding='utf-8')
    return when, has_audio


def cleanup_audio(now):
    for d in (SITE / 'issues').glob('*'):
        try:
            when = dt.datetime.strptime(d.name, '%Y%m%d-%H%M').replace(tzinfo=JST)
        except ValueError:
            continue
        if now - when > dt.timedelta(days=AUDIO_DAYS):
            for f in ('audio.mp3', 'vocab.mp3'):
                if (d / f).exists():
                    (d / f).unlink(); log('古い音声を削除:', d.name, f)


ARCHIVE = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>短期予報解説 バックナンバー</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@100..125,400..800&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{color-scheme:light dark;--bg:light-dark(#fff,#0f1217);--ink:light-dark(#10131a,#e8ebf0);--mute:light-dark(#5b6270,#9aa3b2);--line:light-dark(#e3e6eb,#252b35);--blue:light-dark(#0b4f8a,#7fb6ea);--red:light-dark(#c1121f,#ff6b72)}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
*{box-sizing:border-box;min-width:0}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Noto Sans JP",system-ui,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:0 max(4vw,16px) 40px}
header{border-top:5px solid var(--ink);padding:14px 0 6px}
h1{font:800 26px/1.15 Archivo,"Noto Sans JP",sans-serif;font-stretch:110%;margin:0}
.sub{color:var(--mute);font-size:14px;margin:4px 0 0}
.latest{display:block;margin:18px 0 8px;padding:16px;border:2px solid var(--ink);border-radius:8px;text-decoration:none;color:var(--ink)}
.latest b{display:block;font:800 22px Archivo,"Noto Sans JP",sans-serif}.latest span{color:var(--red);font-weight:700;font-size:14px}
h2{font:700 16px "Noto Sans JP",sans-serif;margin:26px 0 4px;padding-top:10px;border-top:2px solid var(--ink)}
ul{list-style:none;margin:0;padding:0}li a{display:flex;justify-content:space-between;gap:10px;min-height:48px;align-items:center;border-bottom:1px solid var(--line);color:var(--ink);text-decoration:none}
li a .t{font:600 17px Archivo,"Noto Sans JP",sans-serif}li a .m{font-size:13px;color:var(--mute)}
a:focus-visible{outline:3px solid var(--blue);outline-offset:2px}
</style></head><body><div class="wrap">
<header><h1>短期予報解説　日英で読む</h1><p class="sub">気象庁「短期予報解説資料」を英訳・音声つきで。1日2回、自動で更新。</p></header>
%%LATEST%%
%%MONTHS%%
<p class="sub" style="margin-top:28px">原文：気象庁 短期予報解説資料。英訳・単語は生成AIによるもので、誤りを含むことがあります。音声は公開から1か月で削除されます。</p>
</div></body></html>"""


def render_all():
    now = dt.datetime.now(JST)
    cleanup_audio(now)
    items = []
    for f in sorted(DATA.glob('*.json')):
        d = json.loads(f.read_text(encoding='utf-8'))
        when, has = render_issue(d)
        items.append((when, d['id'], has))
    items.sort(reverse=True)
    if not items:
        return
    w, iid, has = items[0]
    latest = f'<a class="latest" href="issues/{iid}/"><span>最新号</span><b>{w.month}月{w.day}日 {w:%H:%M} 発表</b></a>'
    months, cur, buf = [], None, []
    for w, iid, has in items:
        key = f'{w.year}年{w.month}月'
        if key != cur:
            if buf: months.append(f'<h2>{cur}</h2><ul>{"".join(buf)}</ul>')
            cur, buf = key, []
        buf.append(f'<li><a href="issues/{iid}/"><span class="t">{w.month}/{w.day} {w:%H:%M}</span><span class="m">{"音声あり" if has else "文字のみ"}</span></a></li>')
    if buf: months.append(f'<h2>{cur}</h2><ul>{"".join(buf)}</ul>')
    (SITE / 'archive.html').write_text(ARCHIVE.replace('%%LATEST%%', latest).replace('%%MONTHS%%', ''.join(months)), encoding='utf-8')
    (SITE / 'index.html').write_text(
        f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>短期予報解説 最新号</title><meta http-equiv="refresh" content="0; url=issues/{iid}/">'
        f'<link rel="canonical" href="issues/{iid}/"></head><body><p><a href="issues/{iid}/">最新号を開く</a> ／ <a href="archive.html">バックナンバー</a></p></body></html>',
        encoding='utf-8')
    (SITE / '.nojekyll').write_text('')
    log('ページを作成:', len(items), '号')


if __name__ == '__main__':
    c = sys.argv[1] if len(sys.argv) > 1 else 'render'
    if c == 'check':
        iid = issue_id(pdf_text(download_pdf()))[0]
        new = not (DATA / f'{iid}.json').exists()
        log('最新:', iid, '新しい号' if new else '処理済み')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                f.write(f'new={"true" if new else "false"}\nid={iid}\n')
    elif c == 'fetch':
        new = cmd_fetch()
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                f.write(f'new={"true" if new else "false"}\n')
    elif c == 'render':
        render_all()
