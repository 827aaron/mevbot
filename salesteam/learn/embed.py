"""本地语义相似模型（PRD 12.2）：未命中关键词、但与客户教过的好例子意思相近的消息也能找出来。

- 模型：paraphrase-multilingual-MiniLM-L12-v2（Apache-2.0，50+ 语言），Qdrant 发布的 ONNX 量化版（约 235 MB）
- 推理：onnxruntime（MIT）+ tokenizers（Apache-2.0），纯本地 CPU，不花大模型费用
- 首次使用需要下载一次（固定版本 + SHA-256 校验），之后完全离线；第 5 步安装包可直接内置
- 未安装 / 加载失败时，监控员和数据师自动只用关键词，不影响使用
"""
import hashlib
import os
import threading
import urllib.request

from .. import db, paths

MODEL = {
    "id": "mml12-q",
    "name": "paraphrase-multilingual-MiniLM-L12-v2 (ONNX, quantized)",
    "license": "Apache-2.0",
    "repo": "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
    "revision": "faf4aa4225822f3bc6376869cb1164e8e3feedd0",
    "files": {
        "model_optimized.onnx": ("634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99", 235052644),
        "tokenizer.json": ("fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00", 17083009),
    },
}
MATCH_THRESHOLD = 0.6   # 与好例子的余弦相似度达到这个值就算命中（内部默认，客户无需设置）

_lock = threading.Lock()
_sess = None
_tok = None
_dl = {"state": "idle", "done": 0, "total": 0, "error": ""}


def model_dir():
    return paths.DATA_DIR / "models" / MODEL["id"]


def installed() -> bool:
    d = model_dir()
    return all((d / f).exists() and (d / f).stat().st_size == size for f, (_, size) in MODEL["files"].items())


def status() -> dict:
    return {"installed": installed(), "model": MODEL["name"], "license": MODEL["license"],
            "size_mb": round(sum(s for _, s in MODEL["files"].values()) / 1e6), "download": dict(_dl)}


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download():
    """后台下载（来自 Hugging Face，固定提交版本 + SHA-256 校验）。"""
    if _dl["state"] == "downloading":
        return

    def run():
        try:
            d = model_dir()
            d.mkdir(parents=True, exist_ok=True)
            _dl.update(state="downloading", done=0, total=sum(s for _, s in MODEL["files"].values()), error="")
            for fname, (sha, size) in MODEL["files"].items():
                dst = d / fname
                if dst.exists() and dst.stat().st_size == size and _sha(dst) == sha:
                    _dl["done"] += size
                    continue
                url = f"https://huggingface.co/{MODEL['repo']}/resolve/{MODEL['revision']}/{fname}"
                tmp = dst.with_suffix(dst.suffix + ".part")
                with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        _dl["done"] += len(chunk)
                if _sha(tmp) != sha:
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"checksum mismatch: {fname}")
                os.replace(tmp, dst)
            _dl["state"] = "done"
        except Exception as e:  # noqa: BLE001
            _dl.update(state="error", error=str(e)[:200])
    threading.Thread(target=run, daemon=True, name="embed-download").start()


def _load():
    global _sess, _tok
    with _lock:
        if _sess is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            d = model_dir()
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            _sess = ort.InferenceSession(str(d / "model_optimized.onnx"), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
            _tok = Tokenizer.from_file(str(d / "tokenizer.json"))
            _tok.enable_truncation(256)
            _tok.enable_padding()
        return _sess, _tok


def encode(texts: list[str]):
    import numpy as np
    sess, tok = _load()
    out = []
    for i in range(0, len(texts), 32):
        enc = tok.encode_batch([t or " " for t in texts[i:i + 32]])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
        hid = sess.run(None, feed)[0]
        v = (hid * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1)
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        out.append(v.astype(np.float32))
    return np.vstack(out) if out else np.zeros((0, 384), dtype=np.float32)


def _sample_vectors(icp_id: int, agent: str):
    import numpy as np
    rows = db.query("SELECT id, polarity, text FROM icp_samples WHERE icp_id=? AND agent=? ORDER BY id DESC LIMIT 500",
                    (icp_id, agent))
    if not rows:
        return None, None
    have = {r["ref_id"]: r["vec"] for r in db.query(
        f"SELECT ref_id, vec FROM embeddings WHERE kind='sample' AND model=? AND ref_id IN "
        f"({','.join('?' * len(rows))})", (MODEL["id"], *[r["id"] for r in rows]))}
    missing = [r for r in rows if r["id"] not in have]
    if missing:
        vecs = encode([r["text"] for r in missing])
        for r, v in zip(missing, vecs):
            b = v.tobytes()
            have[r["id"]] = b
            db.execute("INSERT OR REPLACE INTO embeddings(kind, ref_id, model, vec) VALUES('sample',?,?,?)",
                       (r["id"], MODEL["id"], b))
    pos = [np.frombuffer(have[r["id"]], dtype=np.float32) for r in rows if r["polarity"] > 0]
    neg = [np.frombuffer(have[r["id"]], dtype=np.float32) for r in rows if r["polarity"] < 0]
    return (np.vstack(pos) if pos else None), (np.vstack(neg) if neg else None)


def sample_similarity(icp_id: int, agent: str, texts: list[str]):
    """每条文本与该模板好例子 / 坏例子的最高相似度 [(pos, neg)]；模型未安装或没有好例子时返回 None。"""
    if not texts or not installed():
        return None
    pos, neg = _sample_vectors(icp_id, agent)
    if pos is None:
        return None
    v = encode(texts)
    ps = (v @ pos.T).max(axis=1)
    ns = (v @ neg.T).max(axis=1) if neg is not None else [0.0] * len(texts)
    return [(float(a), float(b)) for a, b in zip(ps, ns)]


def forget_sample(sample_id: int):
    db.execute("DELETE FROM embeddings WHERE kind='sample' AND ref_id=?", (sample_id,))
