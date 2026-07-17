# acestep.cpp API 文档与示例

> 适用版本：hector918/acestep.cpp fork（含 `--offload-vae` / `--preload` / `--output-dir` / `/files` 扩展）。
> 服务地址下文以 `http://127.0.0.1:8085` 为例（llmnet 内网用容器名 `acestep-cpp:8085`）。

---

## 1. 总体模型：异步 job

所有计算端点（`/lm` `/synth` `/understand` `/vae`）都是**异步**的：

1. `POST` 提交请求 → 立即返回 `{"id":"16位hex"}`；
2. 任务进入 FIFO 队列，由**单个 worker 线程串行执行**（GPU 天然串行，排队即可，不会 503）；
3. 轮询 `GET /job?id=X` 看状态（`running` / `done` / `failed` / `cancelled`）；
4. `done` 后 `GET /job?id=X&result=1` 取结果。

注意事项：

- 结果驻留内存，job 池最多保留 **32 个**，超出后最旧的已完成 job 被挤掉（`Job not found`）。及时取件。
- 结果**不落盘**（除非 server 开了 `--output-dir`，见 §8），server 重启后 job 全部丢失。
- 未完成时取 result 返回 `404 {"error":"Result not ready"}`；轮询 status 无副作用，随便查。
- 请求体上限 256 MB。
- 错误统一为 `{"error":"..."}`，配 400 / 404 / 413 / 500 / 501 / 503 状态码。

标准轮询模板（bash）：

```bash
ID=$(curl -s -X POST http://127.0.0.1:8085/lm -H 'Content-Type: application/json' \
     -d '{"caption":"lofi hiphop, chill","duration":30}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
while [ "$(curl -s "http://127.0.0.1:8085/job?id=$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')" = running ]; do sleep 1; done
curl -s "http://127.0.0.1:8085/job?id=$ID&result=1" -o result.json
```

> shell 陷阱：URL 里的 `&` 必须加引号；`ID=xxx curl ...` 同一行不会导出变量。

---

## 2. 探活与配置

### GET /health

```bash
curl -s http://127.0.0.1:8085/health
# {"status":"ok"}
```

### GET /props

返回可用模型列表、adapter 列表、server 配置、**全部请求字段的默认值**（`default` 节就是权威字段表）、turbo/sft 预设。WebUI 启动时就靠它填下拉框。

```bash
curl -s http://127.0.0.1:8085/props | python3 -m json.tool | head -40
```

```json
{
  "version": "9294213 (2026-07-17)",
  "models": {
    "lm": ["acestep-5Hz-lm-0.6B-Q8_0.gguf"],
    "embedding": ["Qwen3-Embedding-0.6B-Q8_0.gguf"],
    "dit": ["acestep-v15-turbo-Q8_0.gguf"],
    "vae": ["vae-BF16.gguf"]
  },
  "adapters": [],
  "cli": { "max_batch": 1 },
  "default": { "caption": "", "duration": 0, "lm_temperature": 0.85, "...": "..." },
  "presets": {
    "turbo": { "inference_steps": 8,  "guidance_scale": 1.0, "shift": 3.0 },
    "sft":   { "inference_steps": 50, "guidance_scale": 1.0, "shift": 1.0 }
  }
}
```

### GET /logs

SSE 实时流（server 的 stderr，含各阶段进度），浏览器或 `curl -N` 直接看：

```bash
curl -N http://127.0.0.1:8085/logs
```

---

## 3. POST /lm — 作曲规划（caption → 元数据 + 歌词 + audio_codes）

输入 JSON（AceRequest，字段可省略，省略 = 用默认值 = "让 LM 补全"）。`caption` 必填。

常用字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `caption` | — | 风格描述，英文逗号标签（必填） |
| `lyrics` | `""` | `""`=LM 写歌词；`"[Instrumental]"`=纯音乐；其他=用户歌词原样保留 |
| `duration` | 0 | 秒；0=LM 自选（10-600） |
| `bpm` / `keyscale` / `timesignature` | 0/空 | 空=LM 补全，填了=不动 |
| `vocal_language` | `""` | `"zh"` 等 BCP-47；空=LM 检测 |
| `lm_mode` | `generate` | `generate`=全流程含 codes；`inspire`/`format`=只出元数据+歌词，不出 codes |
| `lm_batch_size` | 1 | 一次出 N 个不同版本（受 server `--max-batch` 上限约束） |
| `lm_temperature` | 0.85 | |
| `lm_cfg_scale` | 2.0 | Phase 2 CFG |
| `seed` / `lm_seed` | -1 | -1=随机 |
| `use_cot_caption` | true | true=LM 润色 caption；false=保留原文 |
| `lm_model` | `""` | 指定 LM 文件名，空=当前/第一个 |

**结果**：JSON **数组**（即使 batch=1），每个元素是补全后的完整 AceRequest（含 `audio_codes`、resolved seed），**原样转发给 /synth 即可**。

```bash
curl -s -X POST http://127.0.0.1:8085/lm -H 'Content-Type: application/json' -d '{
  "caption": "Operatic, mezzo-soprano, orchestral, harp, strings, slow tempo, cinematic",
  "lyrics": "[Verse 1]\n(mezzo-soprano, gentle harp)\n在寂静的星空下 我听见你的呼唤",
  "duration": 60,
  "vocal_language": "zh",
  "lm_mode": "generate"
}'
# → {"id":"10e5b631cc981cc8"}，轮询后 result 形如：
# [{"caption":"...enriched...","lyrics":"...","bpm":72,"duration":60,
#   "keyscale":"C major","audio_codes":"3101,11837,27514,...","seed":555601209,...}]
```

---

## 4. POST /synth — 合成（codes/任务 → 音频）

### 4.1 输入形式

**纯 JSON**：单个对象 `{}` 或数组 `[{},{}]`（数组=一个 GPU batch 出多首）。通常直接把 /lm 的结果元素加上 `output_format` 转发：

```bash
# result.json 是 /lm 的结果数组
python3 - <<'EOF'
import json,urllib.request
s=json.load(open("result.json"))[0]; s["output_format"]="mp3"
r=urllib.request.Request("http://127.0.0.1:8085/synth",
    data=json.dumps(s).encode(),headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(r).read().decode())   # {"id":"..."}
EOF
```

**multipart/form-data**（带源音频/latent 的任务）：

| part | 说明 |
|---|---|
| `request` | AceRequest JSON 文本（必填） |
| `audio` | 源音频 WAV/MP3，任意采样率（cover/repaint/lego/extract/complete 用） |
| `src_latents` | 预编码 latent（raw f32 `[T,64]`），提供则跳过 VAE 编码，优先于 `audio` |
| `ref_audio` | 音色参考音频（可选，任意任务可用） |
| `ref_latents` | 音色参考 latent，优先于 `ref_audio` |

```bash
curl -s -X POST http://127.0.0.1:8085/synth \
  -F 'request={"task_type":"cover","caption":"Jazz piano cover, brushed drums","lyrics":"[Instrumental]"}' \
  -F 'audio=@song.mp3'
```

### 4.2 合成相关字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `output_format` | `mp3` | `mp3` / `wav16` / `wav24` / `wav32` |
| `mp3_bitrate` | 128 | kbps |
| `inference_steps` | 0=auto | turbo→8，base/sft→50 |
| `guidance_scale` | 0=auto | →1.0；turbo 上 >1 无意义 |
| `shift` | 0=auto | turbo→3.0，base/sft→1.0 |
| `solver` | `euler` | `euler` / `sde` / `dpm3m` / `stork4` |
| `synth_batch_size` | 1 | 同一请求出 N 个变体（seed 递增），单 batch 上限 9 |
| `seed` | -1 | 每个 batch 项 seed = base+i |
| `task_type` | `text2music` | 见 4.3 |
| `audio_cover_strength` | 1.0 | cover：多少比例的 DiT 步看得到源音频 |
| `cover_noise_strength` | 0.0 | cover：初始噪声与源 latent 混合度 |
| `repainting_start`/`_end` | 0 / -1 | repaint/lego 区域（秒）；负 start=向前外扩，end 超时长=向后外扩 |
| `track` | `""` | lego/extract/complete 的声部名（vocals/drums/bass/guitar/...） |
| `synth_model` / `adapter` / `adapter_scale` | 空/空/1.0 | 模型与 LoRA 选择 |

### 4.3 任务类型

| task_type | 需要源音频 | turbo 可用 | 说明 |
|---|---|---|---|
| `text2music` | 否 | ✓ | 标准生成（codes 或纯 DiT） |
| `cover` | ✓ | ✓ | FSQ 往返降质源→自由翻唱 |
| `cover-nofsq` | ✓ | ✓ | 干净 latent→贴近原曲的 remix（建议 ref_audio=src） |
| `repaint` | ✓ | ✓ | 重绘时间区域 / 外扩（outpaint） |
| `lego` | ✓ | ✗（需 base） | 在伴奏上叠加新声部 |
| `extract` | ✓ | ✗（需 base） | 分离声部 |
| `complete` | ✓ | ✗（需 base） | 单声部补全整曲 |

### 4.4 结果：multipart/mixed

边界 `--ace-batch-boundary`，**每条 track 一个音频 part + 一个 latent part**，按顺序配对：

```
--ace-batch-boundary
Content-Type: audio/mpeg          ← 或 audio/wav

<mp3字节流>
--ace-batch-boundary
Content-Type: application/octet-stream
Content-Disposition: form-data; name="latent"

<raw f32 [T,64] latent>
--ace-batch-boundary--
```

Python 解析：

```python
data = open("out.bin","rb").read()
audios, latents = [], []
for p in data.split(b"--ace-batch-boundary"):
    if b"audio/mpeg" in p or b"audio/wav" in p:
        audios.append(p.split(b"\r\n\r\n",1)[1].rsplit(b"\r\n",1)[0])
    elif b'name="latent"' in p:
        latents.append(p.split(b"\r\n\r\n",1)[1].rsplit(b"\r\n",1)[0])
open("song.mp3","wb").write(audios[0])
open("song.vae","wb").write(latents[0])   # 留着可免费重解码，见 §6
```

---

## 5. POST /understand — 反向：音频 → 元数据+歌词+codes

仅 multipart。`audio` 或 `src_latents` 必给其一；`request` part 可选（模型选择、采样参数，默认 temperature=0.3）。

```bash
curl -s -X POST http://127.0.0.1:8085/understand -F 'audio=@some_song.mp3'
# → {"id":"..."}
```

结果是 multipart/mixed：**一个 JSON part**（caption/bpm/keyscale/duration/语言/歌词/audio_codes——可直接回喂 /synth 重新合成）+ **一个 latent part**（源音频的 VAE latent）。

> 8G 卡 + `--offload-vae` 模式下少混用此接口（VAE-Enc 缓冲会临时叠高峰值）。

---

## 6. POST /vae — 独立 VAE 编解码

仅 multipart，`audio`（编码）与 `src_latents`（解码）二选一，互斥：

```bash
# 编码：音频 → latent（result 为 raw f32 裸流，直接存 .vae）
curl -s -X POST http://127.0.0.1:8085/vae -F 'audio=@song.mp3'

# 解码：latent → 音频（想换格式/码率时用，不用重跑 DiT，几秒出结果）
curl -s -X POST http://127.0.0.1:8085/vae \
  -F 'request={"output_format":"wav24"}' \
  -F 'src_latents=@song.vae'
```

**latent 通用格式**（全 API 一致）：raw f32 小端、扁平 `[T, 64]`、无文件头；`T = 字节数/256`；25Hz，1 帧=1920 音频采样（48kHz）；上限 T≤15000（10 分钟），超出 413。

---

## 7. GET/POST /job — 任务生命周期

```bash
curl -s "http://127.0.0.1:8085/job?id=$ID"              # {"status":"running|done|failed|cancelled"}
curl -s "http://127.0.0.1:8085/job?id=$ID&result=1"     # 取结果（未就绪 404）
curl -s -X POST "http://127.0.0.1:8085/job?id=$ID&cancel=1"   # 取消（LM/DiT 步间检查）
```

---

## 8. /files — 产物目录接件 API（本 fork 扩展）

server 需以 `--output-dir <dir>` 启动（容器默认 `/output`，挂载到宿主机 `/home/audio/output`）。每个 synth job 完成时自动落盘：

```
20260717-201530_<jobid>_<n>.mp3    ← 音频（按 output_format 也可能是 .wav）
20260717-201530_<jobid>_<n>.json   ← 该 track 的完整请求元数据（含 seed，可复现）
```

`--output-max-files N`（容器默认 500）：每次写入后按 mtime 删最旧，目录永不膨胀。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/files` | JSON 列表，新→旧：`[{"name":"...","size":123},...]` |
| GET | `/files?name=X` | 下载单个文件（mp3/wav/json 自动 mime） |
| DELETE | `/files?name=X` | 删除（消费方取走后调用） |

消费方轮询接件的完整循环（Python，stdlib）：

```python
import json, time, urllib.request
B = "http://127.0.0.1:8085"
while True:
    files = json.load(urllib.request.urlopen(f"{B}/files"))
    for f in files:
        if not f["name"].endswith(".mp3"):
            continue
        name = f["name"]
        data = urllib.request.urlopen(f"{B}/files?name={name}").read()
        meta = json.load(urllib.request.urlopen(f"{B}/files?name={name[:-4]}.json"))
        open(name, "wb").write(data)
        print("picked", name, meta.get("bpm"), meta.get("caption", "")[:40])
        for n in (name, name[:-4] + ".json"):     # 取走即删
            req = urllib.request.Request(f"{B}/files?name={n}", method="DELETE")
            urllib.request.urlopen(req)
    time.sleep(10)
```

---

## 9. 端到端完整示例（两段式生成）

仓库自带 `examples/testgen.py`（带分阶段计时与多轮对比），最简用法：

```bash
python3 examples/testgen.py                      # 默认中文歌 60s
python3 examples/testgen.py -n 2                 # 连跑两轮，验证热缓存
python3 examples/testgen.py -c "lofi hiphop, mellow, jazzy chords" \
        -l "[Instrumental]" -d 45 -o /tmp/lofi   # 纯音乐
```

最小化裸代码版：

```python
import json, time, urllib.request
B = "http://127.0.0.1:8085"

def post(p, d):
    r = urllib.request.Request(B+p, data=json.dumps(d).encode(),
                               headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(r))

def wait(jid):
    while True:
        st = json.load(urllib.request.urlopen(f"{B}/job?id={jid}"))["status"]
        if st == "done":
            return urllib.request.urlopen(f"{B}/job?id={jid}&result=1").read()
        assert st == "running", f"job {st}"
        time.sleep(1)

song = json.loads(wait(post("/lm", {"caption": "city pop, female vocal, 80s, groovy",
                                    "duration": 60, "vocal_language": "zh"})["id"]))[0]
song["output_format"] = "mp3"
body = wait(post("/synth", song)["id"])
for p in body.split(b"--ace-batch-boundary"):
    if b"audio/mpeg" in p:
        open("song.mp3", "wb").write(p.split(b"\r\n\r\n",1)[1].rsplit(b"\r\n",1)[0])
        break
```

---

## 10. 本 fork 的 server 启动参数（与上游差异）

| 参数 | 说明 |
|---|---|
| `--offload-vae` | 除 VAE 外全部模型永久常驻（隐含 --keep-loaded）；每首歌只付 ~3s VAE 重载。8G 卡推荐 |
| `--preload` | 启动时预加载全部模型（隐含 --keep-loaded），装不下启动即报错 |
| `--output-dir <dir>` | 产物落盘目录（启用 §8 的 /files API） |
| `--output-max-files <N>` | 产物 sweep 上限 |
| `--max-seq <N>` | LM KV cache 长度；每 1024 约占 235MB 显存（0.6B×2组），8G 卡建议 3072 |

容器默认 CMD：`--offload-vae --preload --max-seq 3072 --vae-chunk 128 --output-dir /output --output-max-files 500`。

生效验证：`docker logs acestep-cpp | grep -E "policy|Preload|Output dir"` 应见 `policy=VAE-OFFLOAD`、`Preload: 6 loaded, 0 failed`、`Output dir: /output`。
