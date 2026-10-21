# Daily Shorts Factory - works in Google Colab (paste into ONE cell) and on GitHub Actions (runs daily by itself).
# Keys needed are listed at the bottom of this file.
import subprocess, sys
IN_COLAB = "google.colab" in sys.modules
if IN_COLAB:
    subprocess.run([sys.executable, "-m", "pip", "-q", "install", "anthropic", "google-genai", "edge-tts>=7.0.0", "nest_asyncio", "requests"], check=True)

# ===== Part 1 =====
# 2. Keys
import os, re, json, time, random, asyncio, subprocess, getpass, pathlib, traceback, datetime, signal, contextlib
import requests

@contextlib.contextmanager
def time_limit(seconds, label):
    """Stops anything that hangs: raises TimeoutError instead of waiting for hours."""
    try:
        def handler(signum, frame):
            raise TimeoutError(f"{label} took longer than {int(seconds)}s and was stopped")
        prev = signal.getitimer(signal.ITIMER_REAL)[0]
        limit = min(seconds, prev) if prev > 0 else seconds
        old_handler = signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, limit)
    except (AttributeError, ValueError):
        yield                      # no alarm available here, run without a limit
        return
    started = time.time()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if prev > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.01, prev - (time.time() - started)))

def get_key(name, required=True):
    try:
        from google.colab import userdata
        v = userdata.get(name)
        if v and v.strip():
            return v.strip()          # strip() removes a hidden line break or space pasted with the key
    except Exception:
        pass
    v = os.environ.get(name)
    if v and v.strip():
        return v.strip()
    if not required:
        return None
    if not IN_COLAB and not sys.stdin.isatty():
        raise RuntimeError(f"Missing secret {name}. Add it in GitHub: Settings > Secrets and variables > Actions.")
    return getpass.getpass(f"Paste {name} and press Enter: ").strip()

ANTHROPIC_API_KEY = get_key("ANTHROPIC_API_KEY")
GEMINI_API_KEY    = get_key("GEMINI_API_KEY")
PIXABAY_API_KEY   = get_key("PIXABAY_API_KEY")
PEXELS_API_KEY    = get_key("PEXELS_API_KEY", required=False)   # optional: Pexels has paused new keys
TG_TOKEN          = get_key("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID        = get_key("TELEGRAM_CHAT_ID")
print("Keys loaded.")

# ===== Part 2 =====
# 3. Settings (edit these)
CLAUDE_MODEL = "claude-sonnet-5-5"     # script writer
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3-flash", "gemini-2.5-flash"]   # fact-checker: tries each until one works
VOICE        = "en-US-AndrewNeural"    # other options: en-US-GuyNeural, en-GB-RyanNeural, en-US-AriaNeural
VOICE_RATE   = "+5%"
CTA          = "Follow for a new story every day."   # spoken at the end of every video

# One entry per video. 10 entries = 10 attempts a day (some may be rejected by the fact-checker, so expect about 5-8 to arrive).
DAILY_PLAN = ["true_crime", "mystery", "history", "did_you_know", "sports",
              "mystery", "true_crime", "history", "did_you_know", "sports"]
MAX_ATTEMPTS_PER_VIDEO = 3
GEMINI_USE_SEARCH = True    # Gemini checks facts with Google Search first (falls back automatically if it is over quota)
SEND_PREVIEW = True         # sends the video as a playable message (correct vertical shape)
SEND_ORIGINAL_FILE = True   # also sends the untouched full-quality file, use THIS one to upload

WORKDIR = pathlib.Path("/content/shorts") if IN_COLAB else pathlib.Path("work")
WORKDIR.mkdir(exist_ok=True)

# Remember past topics so you never get repeats
if IN_COLAB:
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        HISTORY_PATH = pathlib.Path("/content/drive/MyDrive/shorts_history.json")
    except Exception:
        HISTORY_PATH = pathlib.Path("/content/shorts_history.json")
else:
    HISTORY_PATH = pathlib.Path("shorts_history.json")   # GitHub saves this file back to your repo after each run

def load_history():
    try:
        return json.loads(HISTORY_PATH.read_text())
    except Exception:
        return {}

def save_history(h):
    for k in h:
        h[k] = h[k][-200:]
    HISTORY_PATH.write_text(json.dumps(h, indent=1))

print("History file:", HISTORY_PATH)

# ===== Part 3 =====
# 4. Script writer (Claude)
import anthropic
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0, max_retries=2)

NICHES = {
    "did_you_know": {
        "label": "Did you know? (surprising, verifiable facts)",
        "guidance": "One surprising, well-documented fact from science, nature, history or everyday life, explained with 2-3 supporting details. Only use facts you are highly confident are true and widely documented.",
    },
    "history": {
        "label": "History stories",
        "guidance": "A short, gripping true story from history with a clear beginning, twist and ending. Well-documented events only. Use plain language and keep dates and numbers accurate.",
    },
    "true_crime": {
        "label": "Unsolved true crime",
        "guidance": ("A well-known, widely documented UNSOLVED case (at least 10 years old, still unsolved as far as you know). "
                     "Stick to established public facts. Never state or imply that a named real person is guilty. Do not name private individuals who were only suspects or witnesses. "
                     "No graphic detail about violence or injuries. Respectful tone toward victims and families. Avoid cases involving child victims. "
                     "Shots must NOT show identifiable people: use places, objects, weather, empty streets, buildings, night scenes."),
    },
    "mystery": {
        "label": "Unsolved mysteries (unexplained, not crimes)",
        "guidance": ("A famous, well-documented UNSOLVED or unexplained mystery from history, science or the world "
                     "(lost ships, strange signals, ancient objects, vanished places, unexplained events). "
                     "Say clearly what is known, what is disputed and what is still unexplained. Never present speculation as fact and avoid conspiracy claims. "
                     "Do not accuse any real person of anything. Shots must be generic (ocean, old maps, fog, ruins, night sky, desert)."),
    },
    "sports": {
        "label": "Sports stories",
        "guidance": ("A true, well-documented sports story (underdog win, comeback, record, unusual moment). Use only facts you are highly confident about. "
                     "Do not make claims about private lives of real athletes. Shots must be generic (stadium crowd, running track, ball on grass, empty arena), since real match footage is copyrighted."),
    },
}

# Your own topic list. The script uses these first (one per video, never twice), then falls back to
# letting Claude choose. Add or delete lines freely. Keep each one short and specific.
TOPIC_BANK = {
    "mystery": [
        "The Kryptos sculpture and its unsolved fourth passage",
        "The Tunguska event of 1908",
        "The Dancing Plague of 1518",
        "The Hessdalen lights in Norway",
        "The Nazca Lines and why they were made",
        "The Rohonc Codex",
        "The Beale ciphers",
        "The Oak Island Money Pit",
        "The Green Children of Woolpit",
        "The Taos Hum",
        "Cicada 3301 internet puzzles",
        "The Kensington Runestone",
    ],
    "true_crime": [
        "Louis Le Prince, who vanished from a train in 1890",
        "The Lead Masks Case, Brazil 1966",
        "The disappearance of Jim Thompson, the Thai silk king, 1967",
        "The Zodiac Killer's unsolved ciphers",
        "The disappearance of Amelia Earhart, 1937",
        "Percy Fawcett's vanishing in the Amazon, 1925",
        "The 1978 Lufthansa heist at JFK airport",
        "The mystery of Mary Rogers, 1841",
        "The disappearance of Prime Minister Harold Holt, 1967",
        "The disappearance of Jimmy Hoffa, 1975",
    ],
    "history": [
        "The Great Emu War of 1932",
        "The Halifax Explosion of 1917",
        "The Year Without a Summer, 1816",
        "The Zimmermann Telegram",
        "Juan Pujol Garcia, the double agent code-named Garbo",
        "Wojtek, the bear who served in the Polish army",
        "Joshua Norton, Emperor of the United States",
        "The Carrington Event of 1859",
        "The Christmas Truce of 1914",
    ],
    "sports": [
        "Eddie the Eagle at the 1988 Winter Olympics",
        "Derek Redmond finishing the 1992 Olympic 400 metres",
        "Eric Liddell and the 1924 Olympics",
        "The Jamaican bobsled team at the 1988 Olympics",
        "Bob Beamon's 1968 long jump",
        "The Miracle on Ice, 1980",
        "Greece winning Euro 2004",
        "Dick Fosbury and the Fosbury Flop",
    ],
    "did_you_know": [
        "Tardigrades surviving in space",
        "The Eiffel Tower grows taller in summer",
        "Antarctica is the world's largest desert",
        "Wood frogs freeze solid and thaw back to life",
        "Methuselah, the ancient bristlecone pine",
        "Crows recognising human faces",
    ],
}

def next_bank_topic(niche, history):
    used = history.get("_bank_used", [])
    for t in TOPIC_BANK.get(niche, []):
        if t not in used:
            return t
    return None

SCRIPT_SYSTEM = """You write scripts for faceless vertical short videos (YouTube Shorts, TikTok, Reels).

Rules:
- Voiceover: 70 to 95 spoken words (about 30 seconds). Start with a hook sentence that creates curiosity in the first 2 seconds. Short, punchy sentences. No filler. Do NOT include a follow/subscribe line, it is added automatically.
- Wrap 3 to 6 key words or numbers in **double asterisks** so they get highlighted in the subtitles.
- Facts must be accurate. If you are not highly confident of a number or date, leave it out or say "about".
- shots: 5 or 6 stock-footage search queries (1 to 3 words each) in the same order as the voiceover. They must describe generic, real, filmable things (places, objects, nature, weather, textures). Never use names of real people, teams, brands or specific events, and avoid close-ups of identifiable people. Stock sites will not have them.
- facts_to_verify: the 2 to 4 most important factual claims a human should double-check.

Return ONLY valid JSON, no markdown fences, with exactly these keys:
{"topic": "short topic label", "title": "max 70 characters, curiosity-driven", "hook": "the first sentence", "voiceover": "full narration with **highlights**", "shots": ["query 1", "query 2"], "description": "1-2 sentences for the video description", "hashtags": ["#shorts", "#..."], "facts_to_verify": ["claim 1", "claim 2"]}"""

REQUIRED_KEYS = ["topic", "title", "hook", "voiceover", "shots", "description", "hashtags", "facts_to_verify"]

def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON found in model reply")
    return json.loads(text[start:end + 1])

def check_script(s):
    missing = [k for k in REQUIRED_KEYS if k not in s]
    if missing:
        raise ValueError(f"Script missing keys: {missing}")
    if not isinstance(s["shots"], list) or len(s["shots"]) < 3:
        raise ValueError("Need at least 3 shot queries")
    return s

def generate_script(niche, avoid, forced=None):
    user = (f"Niche: {NICHES[niche]['label']}\nGuidance: {NICHES[niche]['guidance']}\n"
            f"Do NOT repeat or closely resemble these earlier topics: {json.dumps(avoid[-60:])}\n")
    if forced:
        user += (f"Write about THIS specific topic: {forced}. Use only well-documented facts, and say plainly what is "
                 f"uncertain or disputed. Set the JSON \"topic\" field to a short label for it.\n")
    user += "Write one new script now."
    for attempt in range(3):
        extra = "\n\nReply with the JSON object only, starting with { and ending with }." if attempt else ""
        msg = claude.messages.create(model=CLAUDE_MODEL, max_tokens=2500, system=SCRIPT_SYSTEM,
                                     messages=[{"role": "user", "content": user + extra}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return check_script(extract_json(text))
        except Exception as e:
            print(f"  script not usable ({e}). stop_reason={getattr(msg, 'stop_reason', None)}. Reply began: {text[:200]!r}")
    raise ValueError("Claude did not return a usable script after 3 tries")

# ===== Part 4 =====
# 5. Fact-checker and proofreader (Gemini, with Google Search when available)
from google import genai
from google.genai import types
gem = genai.Client(api_key=GEMINI_API_KEY)

PROOF_PROMPT = """You are a strict fact-checker and copy editor for short-form video scripts.
Niche: {NICHE}

Check the script below:
1. Verify every factual claim (names, dates, numbers, places). Use search if available.
2. Fix grammar, clarity and awkward phrasing. Keep it 70-95 spoken words, keep the hook strong, and keep the **double-asterisk** highlights (3-6 of them).
3. Fix any inaccurate claim. If you cannot fix it, or a core claim cannot be confirmed, REJECT.
4. For unsolved true crime, REJECT if the case has since been solved or someone was convicted, if a named real person is presented as guilty without a conviction, or if the content is graphic or disrespectful to victims.
5. Check the shots are generic stock-footage queries (no names of people, teams or brands).

Return ONLY valid JSON, no markdown fences:
{"verdict": "ok" or "fixed" or "reject", "issues": ["short note per problem found or change made"], "script": {same keys as the input, corrected}}
If verdict is "reject", "script" may be omitted.

SCRIPT:
{SCRIPT}"""

_gemini_models_cache = []

def gemini_model_list():
    """Your preferred names first, then any other Flash models your key can actually use."""
    if _gemini_models_cache:
        return _gemini_models_cache
    names = list(GEMINI_MODELS)
    try:
        found = []
        for m in gem.models.list():
            n = (getattr(m, "name", "") or "").replace("models/", "")
            acts = getattr(m, "supported_actions", None) or []
            if "flash" in n and "generateContent" in acts and not any(x in n for x in ("image", "live", "tts", "audio", "embedding")):
                found.append(n)
        names += sorted(found, reverse=True)
    except Exception as e:
        print("  could not list Gemini models:", str(e)[:100])
    _gemini_models_cache.extend(dict.fromkeys(names))
    return _gemini_models_cache

def gemini_call(prompt, search):
    kwargs = {"temperature": 0.2}
    if search:
        kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    cfg = types.GenerateContentConfig(**kwargs)
    last = None
    for m in gemini_model_list()[:5]:
        for wait in (0, 12):                       # one retry after 12 seconds
            if wait:
                time.sleep(wait)
            try:
                with time_limit(150, "Gemini call"):
                    return gem.models.generate_content(model=m, contents=prompt, config=cfg)
            except Exception as e:
                last = e
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    break                          # model name unavailable, try the next model
                if not any(c in msg for c in ("429", "503", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE")):
                    raise                          # a real error, do not hide it
    raise last

def claude_review(prompt):
    msg = claude.messages.create(model=CLAUDE_MODEL, max_tokens=2500,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return extract_json(text)

_gemini_down = {"off": False}

def proofread(script, niche):
    prompt = (PROOF_PROMPT.replace("{NICHE}", NICHES[niche]["label"])
                          .replace("{SCRIPT}", json.dumps(script, ensure_ascii=False, indent=2)))
    if not _gemini_down["off"]:
        quota_errors = 0
        for search in ([True, False] if GEMINI_USE_SEARCH else [False]):
            try:
                resp = gemini_call(prompt, search)
                return extract_json(resp.text or "")
            except Exception as e:
                print(f"  Gemini check failed (search={search}): {str(e)[:150]}")
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    quota_errors += 1
        if quota_errors >= 2 or (quota_errors == 1 and not GEMINI_USE_SEARCH):
            _gemini_down["off"] = True
            print("  Gemini quota is used up. Skipping Gemini for the rest of this run.")
    print("  Claude will fact-check this one instead.")
    out = claude_review(prompt)
    out["issues"] = ["Gemini was unavailable, so only Claude checked this script. Verify every fact yourself before posting."] + list(out.get("issues", []))
    return out

# ===== Part 5 =====
# 6. Voice and subtitles
import nest_asyncio
nest_asyncio.apply()
import edge_tts

def media_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return float(out)

async def _tts(text, mp3_path):
    try:
        comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE)
    events = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                events.append({"text": chunk["text"],
                               "start": chunk["offset"] / 1e7,
                               "end": (chunk["offset"] + chunk["duration"]) / 1e7})
    return events

def parse_tokens(text):
    """Split narration into (word, highlighted?) using the **markers**."""
    tokens, hl = [], False
    for raw in text.split():
        parts = raw.split("**")
        flag = hl
        for _ in range(len(parts) - 1):
            hl = not hl
            if hl:
                flag = True
        tokens.append(("".join(parts), flag))
    return tokens

def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())

def assign_display(events, tokens):
    """Attach display text (with punctuation) and highlight flag to each timed word."""
    i, out = 0, []
    for ev in events:
        disp, flag = ev["text"], False
        for j in range(i, min(i + 3, len(tokens))):
            if norm(tokens[j][0]) == norm(ev["text"]):
                disp, flag = tokens[j][0], tokens[j][1]
                i = j + 1
                break
        out.append({**ev, "disp": disp, "hl": flag})
    return out

def even_events(tokens, dur):
    total = sum(len(w) + 1 for w, _ in tokens)
    t, ev = 0.0, []
    for w, h in tokens:
        d = dur * (len(w) + 1) / total
        ev.append({"text": w, "start": t, "end": t + d, "disp": w, "hl": h})
        t += d
    return ev

def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs % 360000) // 6000:02d}:{(cs % 6000) // 100:02d}.{cs % 100:02d}"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,86,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def build_ass(events, path, per_chunk=3):
    chunks, cur = [], []
    for ev in events:
        cur.append(ev)
        if len(cur) >= per_chunk or re.search(r"[.!?]$", ev["disp"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    lines = [ASS_HEADER]
    for k, ch in enumerate(chunks):
        start = ch[0]["start"]
        if k + 1 < len(chunks):
            nxt = chunks[k + 1][0]["start"]
            end = nxt if nxt - ch[-1]["end"] < 0.5 else ch[-1]["end"] + 0.1
        else:
            end = ch[-1]["end"] + 0.3
        text = " ".join(
            ("{\\c&H0000FFFF&}" + w["disp"].upper() + "{\\c&HFFFFFF&}") if w["hl"] else w["disp"].upper()
            for w in ch)
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}\n")
    pathlib.Path(path).write_text("".join(lines), encoding="utf-8")

def make_voice_and_subs(voiceover, job_dir):
    tokens = parse_tokens(voiceover)
    spoken = " ".join(w for w, _ in tokens)
    mp3 = job_dir / "voice.mp3"
    events = asyncio.run(_tts(spoken, mp3))
    dur = media_duration(mp3)
    if events:
        events = assign_display(events, tokens)
    else:
        print("No word timings returned, using even spacing for subtitles.")
        events = even_events(tokens, dur)
    build_ass(events, job_dir / "subs.ass")
    return mp3, dur

# ===== Part 6 =====
# 7. Real stock clips (Pixabay / Pexels, free licenses) and VERTICAL 1080x1920 video assembly
MAX_CLIP_MB = 120

def pexels_clip(query, used_ids, out_path):
    """Download one clip from Pexels (only if you have a key). Returns an id string, or None."""
    if not PEXELS_API_KEY:
        return None
    headers = {"Authorization": PEXELS_API_KEY}
    for orientation in ("portrait", None):
        params = {"query": query, "per_page": 15, "size": "large"}
        if orientation:
            params["orientation"] = orientation
        r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print("Pexels error", r.status_code, r.text[:120])
            return None
        vids = [v for v in r.json().get("videos", []) if f"pexels{v['id']}" not in used_ids and v.get("duration", 0) >= 3]
        random.shuffle(vids)
        for v in vids:
            files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and f.get("height")]
            big = sorted([f for f in files if f["height"] >= 1920], key=lambda f: f["height"])
            good = big or sorted(files, key=lambda f: -f["height"])
            if not good:
                continue
            data = requests.get(good[0]["link"], timeout=180)
            if data.status_code == 200:
                pathlib.Path(out_path).write_bytes(data.content)
                return f"pexels{v['id']}"
    return None

def pixabay_clip(query, used_ids, out_path):
    """Download the sharpest Pixabay clip that is not huge. Returns an id string, or None."""
    hits = []
    for min_w in (1920, 1280):
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": PIXABAY_API_KEY, "q": query[:100], "per_page": 20,
                                 "safesearch": "true", "video_type": "film", "min_width": min_w}, timeout=30)
        if r.status_code != 200:
            print("Pixabay error", r.status_code, r.text[:120])
            return None
        hits = [h for h in r.json().get("hits", []) if f"pixabay{h['id']}" not in used_ids and h.get("duration", 0) >= 3]
        if hits:
            break
    random.shuffle(hits)
    for h in hits:
        vids = h.get("videos", {})
        cands = [vids[k] for k in ("large", "medium", "small") if vids.get(k, {}).get("url")]
        if not cands:
            continue
        ok = [c for c in cands if 0 < c.get("size", 0) <= MAX_CLIP_MB * 1_000_000]
        pick = max(ok, key=lambda c: c.get("height", 0)) if ok else min(cands, key=lambda c: c.get("size") or 10**12)
        data = requests.get(pick["url"], timeout=300)
        if data.status_code == 200:
            pathlib.Path(out_path).write_bytes(data.content)
            return f"pixabay{h['id']}"
    return None

def find_clip(query, used_ids, out_path):
    for fn in (pexels_clip, pixabay_clip):
        try:
            vid = fn(query, used_ids, out_path)
        except Exception as e:
            print(f"  {fn.__name__} failed:", e)
            vid = None
        if vid:
            return vid
    return None

def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + p.stderr[-1500:])

def probe_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)

# Sharp sources (about 1650px tall or more, e.g. 4K): fill the whole vertical frame, no stretching.
FILL_VF = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,fps=30,setsar=1"
# Lower-res sources: keep the clip sharp at full width over a blurred copy of itself (no soft zoom, no stretching).
FIT_BLUR = ("[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:6,eq=brightness=-0.08[bgb];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=30,setsar=1[v]")

def assemble(job_dir, shots, audio_dur):
    used, clips = set(), []
    for i, q in enumerate(shots):
        path = job_dir / f"clip_{i}.mp4"
        vid = find_clip(q, used, path)
        if vid:
            used.add(vid)
            clips.append(path)
        else:
            print(f"  no clip found for '{q}'")
    if not clips:
        raise RuntimeError("No stock clips could be downloaded")
    seg = audio_dur / len(clips)
    segs = []
    for i, c in enumerate(clips):
        w, h = probe_size(c)
        out = f"seg_{i}.mp4"
        base = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", c.name, "-t", f"{seg:.3f}"]
        if h >= 1650:
            vf = ["-vf", FILL_VF]
        else:
            vf = ["-filter_complex", FIT_BLUR, "-map", "[v]"]
        run(base + vf + ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", out], job_dir)
        segs.append(out)
    (job_dir / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs))
    # Final encode with subtitles. If the file is too big for Telegram (50 MB), try a slightly lower quality.
    for crf in (18, 22, 26):
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-i", "voice.mp3",
             "-vf", "ass=subs.ass", "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-crf", str(crf),
             "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", "final.mp4"], job_dir)
        if (job_dir / "final.mp4").stat().st_size < 48_000_000:
            break
    return job_dir / "final.mp4"

def make_video(script, job_dir):
    narration = script["voiceover"].strip() + " " + CTA
    mp3, dur = make_voice_and_subs(narration, job_dir)
    print(f"  voiceover {dur:.1f}s")
    out = assemble(job_dir, script["shots"], dur)
    w, h = probe_size(out)
    print(f"  video {w}x{h}, {out.stat().st_size / 1e6:.1f} MB")
    return out

# ===== Part 7 =====
# 8. Telegram delivery
def tg_text(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text[:4000]}, timeout=60)
    r.raise_for_status()

def tg_video(path, caption):
    """Playable preview. Width/height/duration are sent so Telegram shows the true 9:16 shape, not a square."""
    dur = int(round(media_duration(path)))
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                          data={"chat_id": TG_CHAT_ID, "caption": caption[:1024], "supports_streaming": "true",
                                "width": 1080, "height": 1920, "duration": dur},
                          files={"video": f}, timeout=900)
    r.raise_for_status()

def tg_document(path, caption, title):
    """The untouched full-quality file (Telegram does not recompress documents). Upload this one."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:40] or "short"
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": TG_CHAT_ID, "caption": caption[:1024]},
                          files={"document": (name + ".mp4", f, "video/mp4")}, timeout=900)
    r.raise_for_status()

def post_text(script, issues):
    tags = list(dict.fromkeys(script["hashtags"] + ["#shorts"]))
    msg = f"{script['title']}\n\n{script['description']}\n\n{' '.join(tags)}"
    check = list(script.get("facts_to_verify", [])) + list(issues or [])
    if check:
        msg += "\n\n--- CHECK BEFORE POSTING ---\n" + "\n".join(f"- {c}" for c in check)
    return msg

# ===== Part 8 =====
# 9. Daily run
def preflight():
    """Quick connection test. Sends the result to Telegram so you can see at once what works."""
    results = {}
    def check(name, fn, seconds=60):
        try:
            with time_limit(seconds, name):
                fn()
            results[name] = "OK"
        except Exception as e:
            results[name] = "FAILED: " + str(e)[:150]
        print(f"  check {name}: {results[name]}")
    check("Telegram", lambda: tg_text("Daily Shorts is starting. Checking your connections..."))
    if results["Telegram"] != "OK":
        raise RuntimeError("Telegram is not working, so nothing can be delivered. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    check("Claude", lambda: claude.messages.create(model=CLAUDE_MODEL, max_tokens=20, messages=[{"role": "user", "content": "Say OK"}]))
    check("Pixabay", lambda: requests.get("https://pixabay.com/api/videos/", params={"key": PIXABAY_API_KEY, "q": "ocean", "per_page": 3}, timeout=20).raise_for_status())
    check("Gemini", lambda: gemini_call("Say OK", False))
    check("Voice", lambda: asyncio.run(_tts("Testing one two three.", WORKDIR / "preflight.mp3")))
    report = "Connection check:\n" + "\n".join(f"- {k}: {v}" for k, v in results.items())
    try:
        tg_text(report)
    except Exception:
        pass
    must_work = [k for k in ("Claude", "Pixabay", "Voice") if results[k] != "OK"]
    if must_work:
        raise RuntimeError("Stopping because these failed: " + ", ".join(must_work) + ". See the Telegram message.")

def run_daily():
    preflight()
    history = load_history()
    today = datetime.date.today().isoformat()
    ok_count = 0
    for n, niche in enumerate(DAILY_PLAN, 1):
        print(f"\n=== Video {n}/{len(DAILY_PLAN)}: {niche} ===")
        if n > 1:
            time.sleep(8)          # small pause so the AI services are not hit too fast
        done = False
        for attempt in range(1, MAX_ATTEMPTS_PER_VIDEO + 1):
            try:
                forced = next_bank_topic(niche, history)
                if forced:
                    print("  chosen topic from your list:", forced)
                with time_limit(300, "writing the script"):
                    script = generate_script(niche, history.get(niche, []), forced)
                print("  topic:", script["topic"])
                with time_limit(420, "fact-checking"):
                    review = proofread(script, niche)
                verdict = review.get("verdict", "reject")
                issues = review.get("issues", [])
                history.setdefault(niche, []).append(script["topic"])
                if forced:
                    history.setdefault("_bank_used", []).append(forced)   # only counted once it was actually checked
                if verdict == "reject":
                    print("  rejected by fact-checker:", issues)
                    continue
                final = check_script(review["script"]) if verdict == "fixed" else script
                job_dir = WORKDIR / f"{today}_{n}_{niche}"
                job_dir.mkdir(exist_ok=True)
                with time_limit(900, "making the video"):
                    video = make_video(final, job_dir)
                with time_limit(900, "sending to Telegram"):
                    if SEND_PREVIEW:
                        tg_video(video, f"Video {n}: {final['title']}")
                    if SEND_ORIGINAL_FILE:
                        tg_document(video, f"Video {n} - full quality file, upload this one", final["title"])
                    tg_text(post_text(final, issues))
                save_history(history)
                ok_count += 1
                done = True
                print("  sent to Telegram")
                break
            except Exception:
                print("  attempt failed:\n", traceback.format_exc()[-800:])
        if not done:
            save_history(history)
            try:
                tg_text(f"Video {n} ({niche}) could not be made today. See the notebook output for details.")
            except Exception:
                pass
    print(f"\nDone. {ok_count}/{len(DAILY_PLAN)} videos sent.")
    try:
        tg_text(f"Finished: {ok_count} of {len(DAILY_PLAN)} videos were sent today.")
    except Exception:
        pass

run_daily()

# ---------------- KEYS NEEDED (Colab: key icon -> Secrets -> Notebook access ON; GitHub: Settings -> Secrets and variables -> Actions) ----------------
# ANTHROPIC_API_KEY    console.anthropic.com
# GEMINI_API_KEY       aistudio.google.com
# PIXABAY_API_KEY      pixabay.com -> log in -> pixabay.com/api/docs/ (key is shown on that page)
# TELEGRAM_BOT_TOKEN   Telegram -> @BotFather -> /newbot
# TELEGRAM_CHAT_ID     message your bot, open https://api.telegram.org/bot<TOKEN>/getUpdates, copy chat id
# PEXELS_API_KEY       optional (Pexels paused new keys) - leave it out
