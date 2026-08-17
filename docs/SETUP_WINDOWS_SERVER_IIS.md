# Setup AI_GENEPA trên Windows Server + IIS

Tài liệu này tóm tắt quy trình cài đặt project AI_GENEPA trên một máy chủ Windows hoàn toàn mới.

## 1. Cài Python 3.11.x 64-bit

Sau khi cài, kiểm tra:

```powershell
python --version
where.exe python
```

Ghi lại đường dẫn Python thật, ví dụ:

```text
C:\Users\tanthinh\AppData\Local\Programs\Python\Python311\python.exe
```

## 2. Cài IIS và HttpPlatformHandler

Sau khi cài HttpPlatformHandler, kiểm tra IIS đã nhận module:

```powershell
& "$env:windir\System32\inetsrv\appcmd.exe" list modules | Select-String "httpPlatform"
```

Phải thấy `httpPlatformHandler` trong kết quả.

## 3. Copy source project lên server

Ví dụ thư mục chạy:

```text
E:\Asoft\AI_GENEPA\AI_GENEPA_SRC
```

## 4. Kiểm tra đường dẫn Python và cập nhật `pyvenv.cfg`

Sau khi cài Python trên server, chạy lệnh sau để lấy chính xác đường dẫn Python thực tế:

```powershell
where.exe python
```

Ví dụ kết quả:

```text
C:\Users\tanthinh\AppData\Local\Programs\Python\Python311\python.exe
```

Sau đó mở file môi trường:

```text
E:\Asoft\AI_GENEPA\AI_GENEPA_SRC\AI_GENEPA\pyvenv.cfg
```

Khi đưa source lên server, cập nhật file `AI_GENEPA\pyvenv.cfg` tương ứng trong thư mục project trên server để trỏ tới Python vừa kiểm tra được.

Ví dụ:

```ini
home = C:\Users\tanthinh\AppData\Local\Programs\Python\Python311
version = 3.11.9
executable = C:\Users\tanthinh\AppData\Local\Programs\Python\Python311\python.exe
```

Lưu ý: `home` là thư mục chứa `python.exe`, còn `executable` là đường dẫn đầy đủ tới `python.exe`.

Sau khi cập nhật `pyvenv.cfg`, active môi trường ảo bằng PowerShell:

```powershell
.\AI_GENEPA\Scripts\Activate.ps1
```

Nếu PowerShell chặn script, chạy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\AI_GENEPA\Scripts\Activate.ps1
```

Khi active thành công, đầu dòng terminal sẽ có dạng:

```text
(AI_GENEPA) PS E:\Asoft\AI_GENEPA\AI_GENEPA_SRC>
```

Sau đó kiểm tra môi trường:

```powershell
python --version
python -c "import sys; print(sys.executable); print(sys.base_prefix)"
```

## 5. Test project trực tiếp trước khi đưa qua IIS

Từ root project:

```powershell
python run_server.py
```

Kiểm tra:

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/docs
```

Nếu bước này chưa chạy được thì chưa cấu hình IIS tiếp.

## 6. Cấu hình `web.config`

Các giá trị quan trọng:

```xml
<httpPlatform
  processPath=".\AI_GENEPA\Scripts\python.exe"
  arguments="-m backend.run_iis_server"
  stdoutLogEnabled="true"
  stdoutLogFile=".\backend\logs\httpplatform-stdout">
```

`PYTHONPATH` phải trỏ đến **root project**, không trỏ vào thư mục venv:

```xml
<environmentVariable name="PYTHONPATH" value="E:\Asoft\AI_GENEPA\AI_GENEPA_SRC" />
```

## 7. Cấu hình IIS Application Pool

Khuyến nghị:

```text
.NET CLR Version            = No Managed Code
Enable 32-Bit Applications = False
Identity                    = ApplicationPoolIdentity
Load User Profile           = True
```

Tạo IIS Site trỏ Physical Path về root project và gán đúng Application Pool.

## 8. Cấp quyền cho IIS App Pool

Giả sử App Pool tên `AI_GENEPA`:

```powershell
icacls "E:\Asoft\AI_GENEPA\AI_GENEPA_SRC" /grant "IIS AppPool\AI_GENEPA:(OI)(CI)RX" /T
icacls "E:\Asoft\AI_GENEPA\AI_GENEPA_SRC\backend\logs" /grant "IIS AppPool\AI_GENEPA:(OI)(CI)M" /T
```

Nếu Python nằm trong profile user như:

```text
C:\Users\tanthinh\AppData\Local\Programs\Python\Python311\python.exe
```

thì cấp quyền đọc/chạy cho App Pool:

```powershell
icacls "C:\Users\tanthinh\AppData\Local\Programs\Python\Python311" /grant "IIS AppPool\AI_GENEPA:(OI)(CI)RX" /T
```

## 9. Recycle App Pool và kiểm tra site

Sau khi chỉnh cấu hình/quyền, recycle Application Pool hoặc restart Site rồi truy cập URL đã bind trong IIS.

Ví dụ:

```text
http://localhost:8022
http://<IP_SERVER>:8022
```

## 10. Kiểm tra log khi IIS không chạy

Log Uvicorn:

```powershell
Get-Content ".\backend\logs\Server.log" -Tail 100
```

Log HttpPlatformHandler:

```powershell
Get-ChildItem ".\backend\logs\httpplatform-stdout*" | Sort-Object LastWriteTime -Descending
```

Nếu gặp lỗi:

```text
No Python at 'C:\Users\tanthinh\AppData\Local\Programs\Python\Python311\python.exe'
```

thì kiểm tra đường dẫn Python thật, `AI_GENEPA\pyvenv.cfg` và quyền NTFS của IIS App Pool đối với thư mục Python.

## Luồng setup chuẩn

```text
Python -> tạo venv trên server -> pip install requirements.txt -> test run_server.py
-> cài HttpPlatformHandler -> cấu hình web.config -> cấp quyền IIS App Pool
-> recycle App Pool -> chạy IIS Site
```
