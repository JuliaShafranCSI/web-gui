from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import os, redis, random, time, re, threading

app = FastAPI()

# Redis
redis_url = os.getenv("REDIS_URL", "redis://redis-stack:6379/0")
r = redis.Redis.from_url(redis_url, decode_responses=True)

# File path
shared_dir = os.getenv("FILES_VOLUME", "/data/shared")
os.makedirs(shared_dir, exist_ok=True)
config_path = os.path.join(shared_dir, "process_config.conf")
file_lock = threading.Lock()

def generate_uid() -> int:
    current_ms = int(time.time() * 1000) % 1_000_000
    return current_ms + random.randint(100000, 999999)

def sanitize_rtsp(url: str) -> str:
    return re.sub(r"rtsp://[^@]*@", "rtsp://@", url, count=1)

FORM_HTML = """<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8'>
  <title>Add Video Source</title>
  <link href='https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css' rel='stylesheet'>
</head>
<body class='bg-light d-flex justify-content-center py-5'>
  <div class='card shadow-sm w-100' style='max-width:800px;'>
    <div class='card-body'>
      <h5 class='mb-4'>Add Video Source</h5>
      <form method='post' action='/add'>
        <div class='mb-4'>
          <label class='form-label'>Video Source</label>
          <input type='url' class='form-control' name='video_url' placeholder='rtsp://...' required>
        </div>
        <div class='d-flex gap-2'>
          <button type='submit' class='btn btn-primary px-4'>Add</button>
          <a href='/' class='btn btn-outline-secondary'>Cancel</a>
        </div>
      </form>
    </div>
  </div>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return FORM_HTML

@app.post("/add")
def add(video_url: str = Form(...)):
    uid = generate_uid()
    clean_url = sanitize_rtsp(video_url)
    line = f"{uid} TYPE=VIDEO;LABEL=Video1;ANALYSIS_TYPE=SMART_PARKING_BRENTWOOD;USERNAME=root;DATA={clean_url};LOCAL_SAVE_PATH=NONE;\n"
    with file_lock:
        # overwrite the file (clear) then write new line
        with open(config_path, "w") as f:
            f.write(line)
    r.publish("video_sources", clean_url)
    return RedirectResponse("/stream", status_code=303)

@app.get("/stream", response_class=HTMLResponse)
def stream():
    return HTMLResponse("""<!DOCTYPE html>
<html><body class='bg-light d-flex justify-content-center py-5'>
<h2>Images will appear here…</h2>
</body></html>""")
