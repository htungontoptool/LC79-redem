# LC79 Web · Hướng dẫn triển khai Render

## 1. Cấu trúc thư mục (phải ở thư mục gốc repo)
```
app.py
index.html
logo.jpg
requirements.txt
render.yaml
.gitignore
```

## 2. Đưa lên GitHub
1. Tạo repo mới trên GitHub (ví dụ `lc79-web`).
2. Giải nén file zip, vào thư mục `lc79-render`.
3. Chạy:
   ```
   git init
   git add .
   git commit -m "LC79 web"
   git branch -M main
   git remote add origin https://github.com/<tên>/lc79-web.git
   git push -u origin main
   ```

## 3. Tạo service trên Render
1. New → Web Service → chọn repo vừa push.
2. Runtime: Python. Root Directory: để trống.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn -w 1 -k gthread --threads 50 -b 0.0.0.0:$PORT app:app`
5. Nếu dùng `render.yaml` thì Render tự đọc cấu hình, chỉ cần điền biến bí mật ở bước 4.

## 4. Biến môi trường
| Tên | Giá trị |
|---|---|
| `DATA_DIR` | `/data` |
| `TELETHON_API_ID` | số API ID từ my.telegram.org |
| `TELETHON_API_HASH` | chuỗi API Hash từ my.telegram.org |

## 5. Gắn ổ đĩa lưu dữ liệu (bắt buộc)
Render → service → Disks → Add Disk: Mount Path `/data`, dung lượng 1 GB.
Không có ổ đĩa, acc, proxy, cấu hình và phiên Tele sẽ mất mỗi lần deploy.

## 6. Kiểm tra sau deploy
- Mở `https://<tên-service>.onrender.com` → phải thấy giao diện.
- Mở `/__version` → phải thấy "lc79 v5 · routes: ...".
- Trên giao diện: thêm proxy trước, sau đó đăng nhập Tele nguồn (nhập SĐT, OTP, 2FA nếu có), rồi thêm tài khoản game.
- Bật bot khi đã có proxy, Tele và tài khoản.

## 7. Lưu ý
- Chỉ dùng 1 worker (`-w 1`). Nhiều worker sẽ chạy bot trùng nhau.
- Gói miễn phí của Render ngủ sau ~15 phút không có truy cập, bot sẽ ngừng. Cần gói trả phí để chạy 24/24.
- Mật khẩu acc lưu trong `state.json` ở ổ `/data`, không commit lên GitHub (đã có trong `.gitignore`).
- Dùng thử với 1 tài khoản trước khi bật toàn bộ.
