using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.SignalR;
using SyncClipboard.Server.Core.Hubs;
using Microsoft.AspNetCore.StaticFiles;
using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.Extensions.Caching.Memory;
using SyncClipboard.Server.Core.Services.History;
using SyncClipboard.Server.Core.Services;

namespace SyncClipboard.Server.Core.Controllers;

[ApiController]
[Authorize]
[Tags("SyncClipboard")]
public class SyncClipboardController(
    IHubContext<SyncClipboardHub, ISyncClipboardClient> _hubContext,
    IMemoryCache _cache,
    ServerEnvProvider _serverEnv,
    HistoryService _historyService) : ControllerBase
{
    private static bool InvalidFileName(string name)
    {
        return name.Contains('\\') || name.Contains('/');
    }

    private static void EnsureFolder(string path)
    {
        if (!Directory.Exists(path))
        {
            Directory.CreateDirectory(path);
        }
    }

    private static void SafeDeleteFolder(string path)
    {
        if (Directory.Exists(path))
        {
            try
            {
                Directory.Delete(path, true);
            }
            catch { }
        }
    }

    [HttpGet("api/time")]
    public DateTimeOffset GetServerTime()
    {
        return DateTimeOffset.Now;
    }

    [HttpGet("api/version")]
    public IActionResult GetVersion()
    {
        return Ok(SyncClipboardProperty.AppVersion);
    }

    [AcceptVerbs("PROPFIND")]
    [Route("")]
    [ApiExplorerSettings(IgnoreApi = true)]
    public IActionResult PropfindRoot()
    {
        return Ok();
    }

    [AcceptVerbs("PROPFIND", "MKCOL")]
    [Route("file")]
    [ApiExplorerSettings(IgnoreApi = true)]
    public IActionResult FileFolderEnsure()
    {
        var path = Path.Combine(_serverEnv.GetDataRootPath(), "file");
        EnsureFolder(path);
        return Ok();
    }

    [HttpDelete("file")]
    public IActionResult DeleteFileFolder()
    {
        var path = Path.Combine(_serverEnv.GetDataRootPath(), "file");
        SafeDeleteFolder(path);
        return Ok();
    }

    private async Task AutoMarkDownloadedIfMatch(string fileName, CancellationToken token)
    {
        var profilePath = Path.Combine(_serverEnv.GetDataRootPath(), "SyncClipboard.json");
        var cacheKey = profilePath;

        // 1. 获取当前 ProfileDto
        if (!_cache.TryGetValue(cacheKey, out ProfileDto? current) || current is null)
        {
            if (!System.IO.File.Exists(profilePath))
                return;

            var text = await System.IO.File.ReadAllTextAsync(profilePath, token);
            current = JsonSerializer.Deserialize<ProfileDto>(text);
            if (current is null) return;
        }

        // 2. 判断是否为文件类型，并且数据名称匹配
        if (current.Type == ProfileType.File && current.HasData && current.DataName == fileName)
        {
            current.IsDownloaded = true;

            // 更新缓存
            _cache.Set(cacheKey, current);

            // 写回文件
            var json = JsonSerializer.Serialize(current);
            await System.IO.File.WriteAllTextAsync(profilePath, json, token);

            // 通知所有客户端
            await _hubContext.Clients.All.RemoteProfileChanged(current);
        }
    }

    [HttpHead("file/{fileName}")]
    [HttpGet("file/{fileName}")]
    public async Task<IActionResult> GetFileFromFolder(string fileName, CancellationToken token)
    {
        if (InvalidFileName(fileName))
            return BadRequest();

        // 新增：尝试自动标记已下载（不抛出异常影响主流程）
        try
        {
            await AutoMarkDownloadedIfMatch(fileName, token);
        }
        catch { }

        try
        {
            var path = await _historyService.GetRecentTransferFile(HistoryService.HARD_CODED_USER_ID, fileName, token);
            return await GetFileInternal(path);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            return BadRequest(ex.Message);
        }
    }

    [HttpPut("file/{fileName}")]
    public async Task<IActionResult> PutFileToFolder(string fileName)
    {
        if (InvalidFileName(fileName))
        {
            return BadRequest();
        }
        var folder = Path.Combine(_serverEnv.GetDataRootPath(), "file");
        EnsureFolder(folder);
        var path = Path.Combine(folder, fileName);
        using var fs = new FileStream(path, FileMode.Create);
        await Request.Body.CopyToAsync(fs);

        _cache.Remove("SyncClipboard.json");
        return Ok();
    }

    [HttpGet("SyncClipboard.json")]
    public async Task<ActionResult<ProfileDto>> GetSyncProfile(CancellationToken token)
    {
        var profilePath = Path.Combine(_serverEnv.GetDataRootPath(), "SyncClipboard.json");
        var cacheKey = profilePath;

        // ---- 辅助函数：启动后台标记任务 ----
        async Task ScheduleMarkAsDownloadedAsync(ProfileDto original)
        {
            // 克隆一个新的对象，以免修改原引用
            var clone = new ProfileDto
            {
                Type = original.Type,
                Hash = original.Hash,
                Text = original.Text,
                HasData = original.HasData,
                DataName = original.DataName,
                Size = original.Size,
                IsDownloaded = true,   // 后台任务中设为 true
                Source = original.Source // 来源
            };

            // 捕获需要用到的路径与服务（注意 IHubContext 和 IMemoryCache 是线程安全的单例）
            var hub = _hubContext;
            var cache = _cache;
            var rootPath = _serverEnv.GetDataRootPath(); // 立即取值，避免 Scope 问题

            // 启动后台任务，无视结果
            _ = Task.Run(async () =>
            {
                try
                {
                    // 更新缓存
                    cache.Set(cacheKey, clone);
                    // 写回文件
                    var json = JsonSerializer.Serialize(clone);
                    await System.IO.File.WriteAllTextAsync(profilePath, json);
                    // 通知其他客户端
                    await hub.Clients.All.RemoteProfileChanged(clone);
                }
                catch
                {
                    // 静默处理，不影响主请求
                }
            }, CancellationToken.None);
        }
        // -----------------------------------

        // 缓存命中
        if (_cache.TryGetValue(cacheKey, out ProfileDto? cachedProfile) && cachedProfile != null)
        {
            // 如果是文本且尚未标记，则在返回后异步标记
            if (cachedProfile.Type == ProfileType.Text && !cachedProfile.IsDownloaded)
            {
                _ = ScheduleMarkAsDownloadedAsync(cachedProfile);
            }
            return Ok(cachedProfile);  // 这次返回的仍然是 false
        }

        // 文件不存在，返回空文本
        if (!System.IO.File.Exists(profilePath))
        {
            var dto = await new TextProfile(string.Empty).ToProfileDto(token);
            _cache.Set(cacheKey, dto);
            return Ok(dto);
        }

        // 从文件加载
        try
        {
            var text = await System.IO.File.ReadAllTextAsync(profilePath, token);
            cachedProfile = JsonSerializer.Deserialize<ProfileDto>(text) ?? new ProfileDto();
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            var dto = await new TextProfile(string.Empty).ToProfileDto(token);
            _cache.Set(cacheKey, dto);
            return Ok(dto);
        }

        // 加载完成后，如果需要标记，同样启动后台任务
        if (cachedProfile!.Type == ProfileType.Text && !cachedProfile.IsDownloaded)
        {
            _ = ScheduleMarkAsDownloadedAsync(cachedProfile);
        }

        _cache.Set(cacheKey, cachedProfile);
        return Ok(cachedProfile);
    }

    [HttpPut("SyncClipboard.json")]
    public async Task<IActionResult> PutSyncProfile([FromBody] ProfileDto dto, CancellationToken token)
    {
        if (dto is null)
        {
            return BadRequest("dto cannot be null");
        }

        // 强制重置下载状态
        dto.IsDownloaded = false;

        if (!string.IsNullOrWhiteSpace(dto.Hash))
        {
            var profile = await _historyService.GetExistingProfileAsync(
                HistoryService.HARD_CODED_USER_ID, dto.Type, dto.Hash, token);

            if (profile != null)
            {
                await SaveAndNotifyCurrentProfile(profile, token);
                return Ok();
            }
        }

        return await CreateAndSaveNewProfile(dto, token);
    }

    [HttpPut("api/mark-downloaded")]
    public async Task<IActionResult> MarkAsDownloaded([FromBody] ProfileDto dto, CancellationToken token)
    {
        var profilePath = Path.Combine(_serverEnv.GetDataRootPath(), "SyncClipboard.json");
        var cacheKey = profilePath;

        // 1. 获取当前缓存的 ProfileDto
        if (!_cache.TryGetValue(cacheKey, out ProfileDto? currentProfile) || currentProfile is null)
        {
            // 缓存未命中，尝试从文件加载
            if (!System.IO.File.Exists(profilePath))
                return NotFound("SyncClipboard.json not found");

            var text = await System.IO.File.ReadAllTextAsync(profilePath, token);
            currentProfile = JsonSerializer.Deserialize<ProfileDto>(text);
            if (currentProfile is null)
                return NotFound("SyncClipboard.json is empty or corrupted");
        }

        // 2. 校验 Hash
        if (currentProfile.Hash != dto.Hash)
            return NotFound("Hash mismatch");

        // 3. 标记已下载
        currentProfile.IsDownloaded = true;

        // 4. 更新缓存
        _cache.Set(cacheKey, currentProfile);

        // 5. 写回文件
        var json = JsonSerializer.Serialize(currentProfile);
        await System.IO.File.WriteAllTextAsync(profilePath, json, token);

        // 6. 通过 SignalR 通知所有客户端状态改变
        await _hubContext.Clients.All.RemoteProfileChanged(currentProfile);

        return Ok();
    }

    [HttpGet("api/file/download")]
    public async Task<IActionResult> DownloadFileByPath([FromQuery] string path, CancellationToken token)
    {
        if (string.IsNullOrWhiteSpace(path))
            return BadRequest("Path is required.");

        // 1. 获取当前 ProfileDto
        var profilePath = Path.Combine(_serverEnv.GetDataRootPath(), "SyncClipboard.json");
        ProfileDto? current;

        if (!_cache.TryGetValue(profilePath, out current) || current is null)
        {
            if (!System.IO.File.Exists(profilePath))
                return NotFound("No current profile.");

            var text = await System.IO.File.ReadAllTextAsync(profilePath, token);
            current = JsonSerializer.Deserialize<ProfileDto>(text);
            if (current is null) return NotFound("Profile corrupted.");
        }

        // 2. 验证路径是否在允许的文件列表中
        var allowedPaths = current.FilePaths;
        if (allowedPaths is null || allowedPaths.Count == 0)
            return BadRequest("No file paths available.");

        // Windows 路径不区分大小写
        var comparer = OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
        if (!allowedPaths.Contains(path, comparer))
            return Unauthorized("Path not in current clipboard file list.");

        // 3. 检查文件是否存在
        if (!System.IO.File.Exists(path))
            return NotFound("File not found on server.");

        // 4. 确定 Content-Type
        new FileExtensionContentTypeProvider().TryGetContentType(path, out string? contentType);

        // 5. 返回文件流（不删除原文件）
        var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return File(stream, contentType ?? "application/octet-stream", Path.GetFileName(path));
    }

    [HttpGet("")]
    [ApiExplorerSettings(IgnoreApi = true)]
    public IActionResult GetRoot()
    {
        return Ok("Server is running.");
    }

    private async Task<IActionResult> CreateAndSaveNewProfile(ProfileDto dto, CancellationToken token)
    {
        var newProfile = Profile.Create(dto);

        if (dto.HasData)
        {
            if (string.IsNullOrEmpty(dto.DataName))
            {
                return BadRequest("DataName cannot be null or empty when HasData is true");
            }

            var fileName = Path.GetFileName(dto.DataName);
            var previousDataPath = Path.Combine(_serverEnv.GetDataRootPath(), "file", fileName);
            if (!System.IO.File.Exists(previousDataPath))
            {
                return NotFound("Transfer data file not found");
            }

            var persistentDir = _serverEnv.GetPersistentDir();
            try
            {
                await newProfile.SetAndMoveTransferData(persistentDir, previousDataPath, token);
            }
            catch when (!token.IsCancellationRequested)
            {
                return BadRequest("Hash is not match data.");
            }
        }

        await _historyService.AddProfile(HistoryService.HARD_CODED_USER_ID, newProfile, token);
        await SaveAndNotifyCurrentProfile(newProfile, token);
        return Ok();
    }

    private async Task SaveAndNotifyCurrentProfile(Profile profile, CancellationToken token)
    {
        var profileDto = await profile.ToProfileDto(token);
        var dataRoot = _serverEnv.GetDataRootPath();

        var profilePath = Path.Combine(dataRoot, "SyncClipboard.json");
        _cache.Set(profilePath, profileDto);
        var profileText = JsonSerializer.Serialize(profileDto);
        await System.IO.File.WriteAllTextAsync(profilePath, profileText, token);

        await _hubContext.Clients.All.RemoteProfileChanged(profileDto);
    }

    private async Task<IActionResult> GetFileInternal(string? path)
    {
        if (string.IsNullOrEmpty(path) || !System.IO.File.Exists(path))
        {
            return File([], "application/octet-stream");
        }

        new FileExtensionContentTypeProvider()
            .TryGetContentType(path, out string? contentType);

        var bytes = await System.IO.File.ReadAllBytesAsync(path);

        try
        {
            System.IO.File.Delete(path);
            // 如果上级目录变空，则删除目录
            var dir = Path.GetDirectoryName(path);
            if (Directory.Exists(dir) && !Directory.EnumerateFileSystemEntries(dir).Any())
            {
                Directory.Delete(dir);
            }
        }
        catch { }
        return File(bytes, contentType ?? "application/octet-stream");
    }
}