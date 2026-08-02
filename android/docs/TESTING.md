# Manual test checklist

Assume the phone IP is `192.168.1.50`, the app listens on `8080`, and the key is `123456`.

## Service health

```bash
curl http://192.168.1.50:8080/ping
```

Expected response: `OK`.

## Push text to Android

```bash
curl -X POST http://192.168.1.50:8080/update/current_latest \
  -H 'Content-Type: application/json' \
  -d '{
    "key":"123456",
    "type":"text",
    "latest_global":{
      "source":"Windows-PC",
      "id":"test-001",
      "content":"5L2g5aW9IFN5bmNCcmlkZ2U=",
      "encode":"base64"
    }
  }'
```

The Android clipboard should become `你好 SyncClipboard`.

## Announce a file through the normal file list

```bash
curl -X POST http://192.168.1.50:8080/update/current_latest \
  -H 'Content-Type: application/json' \
  -d '{
    "key":"123456",
    "type":"file",
    "latest_global":{"source":"Windows-PC","id":"file-test-001"}
  }'
```

A file notification should appear and expire after about 10 seconds. Clicking it calls the configured server's `/request_file` endpoint.

## Direct server-relay file

```bash
curl -X POST 'http://192.168.1.50:8080/upload_file?filename=example.zip' \
  -H 'Content-Type: application/json' \
  -d '{
    "key":"123456",
    "download_url":"http://192.168.1.10:8000/temp/example.zip",
    "clear_url":"http://192.168.1.10:8000/temp/clear/example"
  }'
```

The app should show a notification or download automatically, depending on configuration.

## Android to server

1. Open SyncClipboard and choose one or more files, or share a file from another app to SyncClipboard.
2. Confirm the server receives multipart fields `key`, `source`, `type`, `filename`, `size`, and `file`.
3. If the server endpoint differs, change “文件上传路径” in the app.
