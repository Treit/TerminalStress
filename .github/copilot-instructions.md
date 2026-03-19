# Copilot Instructions

## Build and Run

```bash
# Build
dotnet build src/TerminalStress.sln

# Run (UTF-8 mode, default)
dotnet run --project src/TerminalStress.csproj

# Run (UTF-7 mode, triggered by passing any argument)
dotnet run --project src/TerminalStress.csproj -- anyarg
```

## Architecture

Single-file C# console app (`src/Program.cs`) that stress tests Windows Terminal by running an infinite loop that:

- Randomly positions the cursor and writes random Unicode characters in random console colors
- Periodically clears the screen and dumps accumulated output
- Periodically floods the console with emoji sequences
- Swallows exceptions from invalid cursor positions or write failures and renders emoji error indicators instead

Passing any command-line argument switches the output encoding from UTF-8 to UTF-7.

## Conventions

- Target framework is .NET 7.0 (`net7.0`).
- `#pragma warning disable SYSLIB0001` is used intentionally to allow UTF-7 encoding for stress testing purposes.
- The solution file lives inside `src/` alongside the project and source files.
- Use `uv` instead of `pip` for installing Python packages (e.g., `uv pip install` instead of `pip install`).
