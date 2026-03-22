namespace StressBotService;

using System.Text.Json;
using System.Text.Json.Serialization;

public class ServiceConfig
{
    /// <summary>
    /// Path to python.exe. Relative paths are resolved from WorkingDirectory.
    /// Use .venv\Scripts\python.exe to pick up the virtual environment.
    /// </summary>
    [JsonPropertyName("pythonPath")]
    public string PythonPath { get; set; } = @".venv\Scripts\python.exe";

    /// <summary>
    /// Path to agent_daemon.py. Relative paths are resolved from WorkingDirectory.
    /// </summary>
    [JsonPropertyName("scriptPath")]
    public string ScriptPath { get; set; } = @"src\monkey\agent_daemon.py";

    /// <summary>
    /// Working directory for the daemon process (repo root).
    /// </summary>
    [JsonPropertyName("workingDirectory")]
    public string WorkingDirectory { get; set; } = @"C:\Users\randy\Git\TerminalStress";

    /// <summary>
    /// Extra command-line arguments passed to agent_daemon.py (e.g. "--interval 15 --dry-run").
    /// </summary>
    [JsonPropertyName("arguments")]
    public string Arguments { get; set; } = "";

    /// <summary>
    /// Whether to restart the daemon if it exits unexpectedly.
    /// </summary>
    [JsonPropertyName("restartOnCrash")]
    public bool RestartOnCrash { get; set; } = true;

    /// <summary>
    /// Seconds to wait before restarting after a crash.
    /// </summary>
    [JsonPropertyName("restartDelaySeconds")]
    public int RestartDelaySeconds { get; set; } = 5;

    /// <summary>
    /// Maximum number of restarts within the restart window before giving up.
    /// </summary>
    [JsonPropertyName("maxRestarts")]
    public int MaxRestarts { get; set; } = 10;

    /// <summary>
    /// Time window (minutes) for counting restarts. Resets after this period.
    /// </summary>
    [JsonPropertyName("maxRestartWindowMinutes")]
    public int MaxRestartWindowMinutes { get; set; } = 60;

    /// <summary>
    /// Directory for service log files. Relative paths resolve from WorkingDirectory.
    /// </summary>
    [JsonPropertyName("logDirectory")]
    public string LogDirectory { get; set; } = @"src\monkey_logs";

    /// <summary>
    /// Master switch — if false the service starts but does not launch the daemon.
    /// </summary>
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;

    /// <summary>
    /// Extra directories to prepend to PATH when launching the daemon.
    /// The service runs as SYSTEM and won't have the user's PATH (which has copilot, etc.).
    /// </summary>
    [JsonPropertyName("extraPath")]
    public string ExtraPath { get; set; } = "";

    // --- helpers ---

    private static readonly JsonSerializerOptions s_jsonOpts = new()
    {
        WriteIndented = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    /// <summary>Resolve a potentially-relative path against WorkingDirectory.</summary>
    public string ResolvePath(string path)
    {
        if (Path.IsPathRooted(path))
            return path;
        return Path.GetFullPath(Path.Combine(WorkingDirectory, path));
    }

    public string ResolvedPythonPath => ResolvePath(PythonPath);
    public string ResolvedScriptPath => ResolvePath(ScriptPath);
    public string ResolvedLogDirectory => ResolvePath(LogDirectory);

    public static ServiceConfig Load(string path)
    {
        if (!File.Exists(path))
            return new ServiceConfig();

        var json = File.ReadAllText(path);
        return JsonSerializer.Deserialize<ServiceConfig>(json, s_jsonOpts) ?? new ServiceConfig();
    }

    public void Save(string path)
    {
        var json = JsonSerializer.Serialize(this, s_jsonOpts);
        File.WriteAllText(path, json);
    }

    /// <summary>Find stressbot-service.json next to the running executable.</summary>
    public static string DefaultConfigPath()
    {
        var dir = AppContext.BaseDirectory;
        return Path.Combine(dir, "stressbot-service.json");
    }
}
