# Display ASCII banner
$banner = @'
                    .-._   _ _ _ _ _ _ _ _
         .-''-.__.-'00  '-' ' ' ' ' ' ' ' '-.
         '.___ '    .   .--_'-' '-' '-' _'-' '._
          V: V 'vv-'   '_   '.       .'  _..' '.'.
            '=.____.=_.--'   :_.__.__:_   '.   : :
                    (((____.-'        '-.  /   : :
                                      (((-'\ .' /
                                    _____..'  .'
                                   '-._____.-'   
   ____                               _ _ _ 
  / ___|_ __ _   _ _ __   ___ ___  __| (_) | ___ 
 | |   | '__| | | | '_ \ / __/ _ \/ _` | | |/ _ \
 | |___| |  | |_| | |_) | (_| (_) | (_| | | |  __/
  \____|_|   \__, | .__/ \___\___/\__,_|_|_|\___|
             |___/|_|                            
'@

Write-Host $banner -ForegroundColor Green

Write-Host "=================================================" -ForegroundColor Green
Write-Host "      Crocodile CLI Framework Installer         " -ForegroundColor Green
Write-Host "=================================================" -ForegroundColor Green
Write-Host ""

$tempDir = if ($env:TEMP) { $env:TEMP } else { $env:USERPROFILE }
$LogFile = Join-Path $tempDir "crocodile_install.log"
Remove-Item -Path $LogFile -ErrorAction SilentlyContinue

# Helper function to run steps and handle failures
function Run-Step {
    param (
        [string]$Message,
        [scriptblock]$ScriptBlock
    )
    
    Write-Host "  ⟳  " -NoNewline
    Write-Host ("{0,-45}" -f $Message) -NoNewline
    
    $oldErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Stop"
    try {
        $global:LASTEXITCODE = 0
        & $ScriptBlock >> $LogFile 2>&1
        if ($global:LASTEXITCODE -ne 0 -and $null -ne $global:LASTEXITCODE) {
            throw "Command exited with non-zero exit code: $global:LASTEXITCODE"
        }
        Write-Host "✓ Done" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Failed" -ForegroundColor Red
        Write-Host ""
        Write-Host "Error: Installation failed at step: $Message" -ForegroundColor Red
        Write-Host "Error details (from $LogFile):" -ForegroundColor Red
        Write-Host "------------------------------------------------------------------------" -ForegroundColor Yellow
        if (Test-Path $LogFile) {
            Get-Content $LogFile
        } else {
            Write-Host $_.Exception.Message
        }
        Write-Host "------------------------------------------------------------------------" -ForegroundColor Yellow
        exit 1
    }
    finally {
        $ErrorActionPreference = $oldErrorAction
    }
}

# 1. Verify python is installed and checks that the version is >= 3.12
Write-Host "  ⟳  " -NoNewline
Write-Host ("{0,-45}" -f "Verifying Python 3 version...") -NoNewline

$pythonCmd = $null

# Check common python commands on Windows: py, python, python3
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    # Verify py can run a python 3 version and is >= 3.12
    $versionCheck = & py -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
    if ($versionCheck -eq "True") {
        $pythonCmd = "py"
    }
}

if (-not $pythonCmd -and (Get-Command "python" -ErrorAction SilentlyContinue)) {
    $versionCheck = & python -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
    if ($versionCheck -eq "True") {
        $pythonCmd = "python"
    }
}

if (-not $pythonCmd -and (Get-Command "python3" -ErrorAction SilentlyContinue)) {
    $versionCheck = & python3 -c "import sys; print(sys.version_info >= (3, 12))" 2>$null
    if ($versionCheck -eq "True") {
        $pythonCmd = "python3"
    }
}

if (-not $pythonCmd) {
    Write-Host "✗ Failed" -ForegroundColor Red
    Write-Host ""
    # Check if python exists but is an older version
    $foundPython = $null
    if (Get-Command "python" -ErrorAction SilentlyContinue) { $foundPython = "python" }
    elseif (Get-Command "python3" -ErrorAction SilentlyContinue) { $foundPython = "python3" }
    elseif (Get-Command "py" -ErrorAction SilentlyContinue) { $foundPython = "py" }

    Write-Host "Error: Python 3.12+ was not found on your system." -ForegroundColor Red
    if ($foundPython) {
        $currentVer = & $foundPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
        Write-Host "Found Python version: $currentVer (Minimum required: 3.12)" -ForegroundColor Yellow
    } else {
        Write-Host "Python is not installed." -ForegroundColor Yellow
    }
    
    Write-Host ""
    Write-Host "How to install Python 3.12+ on Windows:" -ForegroundColor White
    Write-Host "----------------------------------------"
    Write-Host "Option 1: Download from python.org"
    Write-Host "  Go to https://www.python.org/downloads/ and download Python 3.12+"
    Write-Host "  Ensure you check 'Add python.exe to PATH' during installation."
    Write-Host ""
    Write-Host "Option 2: Install via Winget (Windows Package Manager)"
    Write-Host "  winget install Python.Python.3.12"
    Write-Host "----------------------------------------"
    exit 1
}

$currentVer = & $pythonCmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
Write-Host "✓ Verified ($currentVer)" -ForegroundColor Green

# 2. Verify Git is installed
Write-Host "  ⟳  " -NoNewline
Write-Host ("{0,-45}" -f "Verifying Git installation...") -NoNewline

if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
    Write-Host "✗ Failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "Error: 'git' is not installed, which is required to download the package from GitHub." -ForegroundColor Red
    Write-Host "Please install git first. For example, using winget:" -ForegroundColor Yellow
    Write-Host "  winget install Git.Git"
    exit 1
}
$gitVersion = (git --version) | Select-Object -First 1
Write-Host "✓ Verified ($gitVersion)" -ForegroundColor Green

# 3. Creates directory $env:USERPROFILE\.crocodile
$crocodileDir = Join-Path $env:USERPROFILE ".crocodile"
Run-Step "Creating directory ~/.crocodile..." {
    if (-not (Test-Path $crocodileDir)) {
        New-Item -ItemType Directory -Path $crocodileDir | Out-Null
    }
}

# 4. Creates virtual environment $env:USERPROFILE\.crocodile\venv
$venvDir = Join-Path $crocodileDir "venv"
Run-Step "Creating virtual environment..." {
    & $pythonCmd -m venv $venvDir
}

# 5. Upgrades pip
$pipExe = Join-Path $venvDir "Scripts\pip.exe"
Run-Step "Upgrading pip..." {
    & $pipExe install --upgrade pip
}

# 6. Installs the CLI package
Run-Step "Installing Crocodile..." {
    # The distribution is `crocodile` as of 0.3.0; pip rejects a requirement whose
    # name does not match the project it resolves to. The repository rename has
    # since landed, so the URL says Crocodile too — Crypcodile.git still redirects,
    # but a redirect is not a thing to depend on.
    & $pipExe install "crocodile[full] @ git+https://github.com/nazmiefearmutcu/Crocodile.git"
}

# 7. Adds $env:USERPROFILE\.crocodile\venv\Scripts to the User PATH environment variable
Run-Step "Updating PATH configuration..." {
    $userPathTarget = [System.EnvironmentVariableTarget]::User
    $currentPath = [System.Environment]::GetEnvironmentVariable("Path", $userPathTarget)
    $targetScriptsPath = Join-Path $venvDir "Scripts"

    $alreadyExists = $false
    if ($currentPath) {
        $paths = $currentPath -split ';'
        foreach ($p in $paths) {
            if ($p.Trim().TrimEnd('\') -eq $targetScriptsPath.Trim().TrimEnd('\')) {
                $alreadyExists = $true
                break
            }
        }
    }

    if (-not $alreadyExists) {
        if ($currentPath -notlike "*\.crocodile\venv\Scripts*") {
            $newPath = $currentPath
            if ($currentPath -and -not $currentPath.EndsWith(';')) {
                $newPath += ";"
            }
            $newPath += $targetScriptsPath
            [System.Environment]::SetEnvironmentVariable("Path", $newPath, $userPathTarget)
        }
    }
}

# 8. Displays prominent notification message
Write-Host ""
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "Crocodile has successfully downloaded!" -ForegroundColor Green
Write-Host "Please restart your terminal/PowerShell session before using it." -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor Green
Write-Host ""
