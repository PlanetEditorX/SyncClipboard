using System.Text.Json.Serialization;
using SyncClipboard.Shared.Profiles;
using SyncClipboard.Shared.Utilities;

namespace SyncClipboard.Shared;

[Obsolete("Use ProfileDto instead")]
public record class ClipboardProfileDTO
{
    [JsonPropertyName(nameof(File))]
    public string File { get; set; }

    [JsonPropertyName(nameof(Clipboard))]
    public string Clipboard { get; set; }

    [JsonPropertyName(nameof(Type))]
    [JsonConverter(typeof(JsonStringEnumConverter))]
    public ProfileType Type { get; set; }

    // 新增属性：存放 Windows 本地原始路径列表
    [JsonPropertyName(nameof(FilePaths))]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public List<string>? FilePaths { get; set; }

    // 构造函数同步更新，支持传入 filePaths
    public ClipboardProfileDTO(
        string file = "",
        string clipboard = "",
        ProfileType type = ProfileType.Text,
        List<string>? filePaths = null)
    {
        File = file;
        Clipboard = clipboard;
        Type = type;
        FilePaths = filePaths;
    }

    [Obsolete("Use Profile.Create(ProfileDto) instead")]
    public static Profile CreateProfile(ClipboardProfileDTO profileDTO, bool ignoreHash = false)
    {
        throw new NotSupportedException(
            "ClipboardProfileDTO is obsolete. Use Profile.Create(ProfileDto) instead.");
    }
}