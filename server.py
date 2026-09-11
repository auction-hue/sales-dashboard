#!/usr/bin/env python3
"""
売上ダッシュボード用のローカルサーバー。
同じフォルダにある「売上データ*.csv」形式のCSVを一覧・取得するAPIと、
ダッシュボードHTML自体を配信する。外部ライブラリ不要(標準ライブラリのみ)。

使い方:
    python server.py [port]   # 省略時は 8000

ブラウザで http://localhost:8000/ を開く。
"""

import csv
import glob
import io
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = "売上ダッシュボード.html"
CSV_PATTERN = "売上データ*.csv"

REQUIRED_COLUMNS = ["日付", "商品名", "カテゴリ", "数量", "売上金額"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def list_csv_files():
    """フォルダ内の対象CSVを走査し、各ファイルの概要(件数・期間)を返す。"""
    files = []
    for path in sorted(glob.glob(os.path.join(BASE_DIR, CSV_PATTERN))):
        name = os.path.basename(path)
        try:
            rows = read_csv_rows(name)
        except Exception:
            continue
        if not rows:
            continue
        dates = sorted(r["d"] for r in rows)
        files.append({
            "filename": name,
            "count": len(rows),
            "dateMin": dates[0],
            "dateMax": dates[-1],
        })
    return files


def read_csv_rows(filename):
    """指定CSVを読み込み、フロント側が期待する形([{d,p,c,q,s}, ...])に変換する。"""
    safe_name = os.path.basename(filename)
    path = os.path.join(BASE_DIR, safe_name)
    if not os.path.isfile(path):
        raise FileNotFoundError(safe_name)
    # フォルダ直下の 売上データ*.csv のみ許可(パストラバーサル対策)
    allowed = {os.path.basename(p) for p in glob.glob(os.path.join(BASE_DIR, CSV_PATTERN))}
    if safe_name not in allowed:
        raise PermissionError(safe_name)

    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"必須列が不足しています: {missing}")
        for row in reader:
            rows.append({
                "d": row["日付"].strip(),
                "p": row["商品名"].strip(),
                "c": row["カテゴリ"].strip(),
                "q": int(row["数量"]),
                "s": int(row["売上金額"]),
            })
    return rows


def read_all_rows():
    """フォルダ内の対象CSVをすべて読み込み、日付順に結合して返す。"""
    all_rows = []
    sources = []
    for path in sorted(glob.glob(os.path.join(BASE_DIR, CSV_PATTERN))):
        name = os.path.basename(path)
        try:
            rows = read_csv_rows(name)
        except Exception:
            continue
        if rows:
            all_rows.extend(rows)
            sources.append(name)
    all_rows.sort(key=lambda r: r["d"])
    return all_rows, sources


def validate_and_parse_csv_text(text):
    """アップロードされたCSVテキストを検証し、行データのリストを返す(不正なら例外)。"""
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"必須列が不足しています: {missing}")
    rows = []
    for i, row in enumerate(reader, start=2):  # 1行目はヘッダー
        d = (row.get("日付") or "").strip()
        if not DATE_RE.match(d):
            raise ValueError(f"{i}行目: 日付の形式が不正です(YYYY-MM-DDで入力してください): {d!r}")
        try:
            q = int(str(row["数量"]).strip())
            s = int(str(row["売上金額"]).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{i}行目: 数量/売上金額が整数ではありません")
        rows.append({
            "d": d,
            "p": (row.get("商品名") or "").strip(),
            "c": (row.get("カテゴリ") or "").strip(),
            "q": q,
            "s": s,
        })
    if not rows:
        raise ValueError("有効なデータ行がありません")
    return rows


def sanitize_filename(name):
    """アップロードされたファイル名を安全化し、売上データ*.csv 形式に正規化する。"""
    name = os.path.basename((name or "").strip()) or "upload.csv"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    if not name.lower().endswith(".csv"):
        name += ".csv"
    if not name.startswith("売上データ"):
        stem = name[:-4]
        name = f"売上データ_{stem}.csv" if stem else "売上データ_upload.csv"
    return name


def unique_path(base_dir, filename):
    """同名ファイルが既にある場合は連番を付けて衝突を避ける。"""
    path = os.path.join(base_dir, filename)
    if not os.path.exists(path):
        return path, filename
    stem, ext = os.path.splitext(filename)
    i = 2
    while True:
        candidate = f"{stem}_{i}{ext}"
        path = os.path.join(base_dir, candidate)
        if not os.path.exists(path):
            return path, candidate
        i += 1


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json({"error": message}, status=status)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_error_json(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            self._send_file(os.path.join(BASE_DIR, DASHBOARD_FILE), "text/html; charset=utf-8")
            return

        if route == "/api/files":
            self._send_json({"files": list_csv_files()})
            return

        if route == "/api/data":
            qs = parse_qs(parsed.query)
            filename = unquote(qs.get("file", [""])[0])
            if not filename:
                self._send_error_json(400, "file クエリパラメータが必要です")
                return
            if filename == "__all__":
                rows, sources = read_all_rows()
                if not rows:
                    self._send_error_json(404, "CSVが見つかりません")
                    return
                self._send_json({"sources": sources, "rows": rows})
                return
            try:
                rows = read_csv_rows(filename)
            except FileNotFoundError:
                self._send_error_json(404, f"ファイルが見つかりません: {filename}")
                return
            except PermissionError:
                self._send_error_json(403, f"許可されていないファイルです: {filename}")
                return
            except Exception as e:
                self._send_error_json(500, str(e))
                return
            self._send_json({"sources": [filename], "rows": rows})
            return

        self._send_error_json(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/upload":
            self._send_error_json(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_error_json(400, "アップロードされたデータが空です")
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_error_json(413, "ファイルサイズが大きすぎます(上限20MB)")
            return

        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self._send_error_json(400, "文字コードがUTF-8ではありません")
            return

        orig_name = unquote(self.headers.get("X-Filename", "upload.csv"))

        try:
            rows = validate_and_parse_csv_text(text)
        except ValueError as e:
            self._send_error_json(400, f"CSVの検証に失敗しました: {e}")
            return

        safe_name = sanitize_filename(orig_name)
        path, final_name = unique_path(BASE_DIR, safe_name)
        with open(path, "wb") as f:
            f.write(raw)

        dates = sorted(r["d"] for r in rows)
        self._send_json({
            "filename": final_name,
            "count": len(rows),
            "dateMin": dates[0],
            "dateMax": dates[-1],
        })


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"売上ダッシュボードサーバー起動: http://localhost:{port}/")
    print(f"対象フォルダ: {BASE_DIR}")
    print("Ctrl+C で停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.shutdown()


if __name__ == "__main__":
    main()
