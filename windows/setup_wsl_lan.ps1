[CmdletBinding()]
param(
    [string]$Distro = "",
    [int]$Port = 8300,
    [string]$RuleName = "agentchattr WSL LAN"
)

function Ensure-Admin {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        return
    }

    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-Port", $Port,
        "-RuleName", "`"$RuleName`""
    )
    if ($Distro) {
        $argList += @("-Distro", "`"$Distro`"")
    }

    Start-Process powershell.exe -Verb RunAs -ArgumentList ($argList -join " ")
    exit 0
}

function Get-WslIpv4 {
    param(
        [string]$TargetDistro
    )

    $wslArgs = @()
    if ($TargetDistro) {
        $wslArgs += @("-d", $TargetDistro)
    }
    $wslArgs += @("hostname", "-I")

    $output = & wsl.exe @wslArgs 2>$null
    if (-not $output) {
        throw "Could not read a WSL IP address. Make sure the distro is running."
    }

    $matches = [regex]::Matches(($output -join " "), '\b(?:\d{1,3}\.){3}\d{1,3}\b')
    foreach ($match in $matches) {
        $ip = $match.Value
        if (-not $ip.StartsWith("127.")) {
            return $ip
        }
    }

    throw "No usable WSL IPv4 address found."
}

function Reset-PortProxy {
    param(
        [string]$ConnectAddress,
        [int]$ListenPort
    )

    & netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$ListenPort | Out-Null
    & netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$ListenPort connectaddress=$ConnectAddress connectport=$ListenPort | Out-Null
}

function Ensure-FirewallRule {
    param(
        [string]$DisplayName,
        [int]$LocalPort
    )

    $existing = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Remove-NetFirewallRule | Out-Null
    }

    New-NetFirewallRule `
        -DisplayName $DisplayName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $LocalPort | Out-Null
}

function Get-HostIpv4s {
    Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.InterfaceAlias -notmatch "WSL|vEthernet|Loopback"
        } |
        Sort-Object InterfaceAlias, IPAddress |
        Select-Object -Unique InterfaceAlias, IPAddress
}

Ensure-Admin

$wslIp = Get-WslIpv4 -TargetDistro $Distro
Reset-PortProxy -ConnectAddress $wslIp -ListenPort $Port
Ensure-FirewallRule -DisplayName $RuleName -LocalPort $Port

$hostIps = Get-HostIpv4s

Write-Host ""
Write-Host "agentchattr WSL LAN access configured"
Write-Host "WSL IP: $wslIp"
Write-Host "Forwarded Windows port: $Port"
Write-Host ""
Write-Host "Phone URLs:"
if ($hostIps) {
    foreach ($entry in $hostIps) {
        Write-Host ("- {0}: http://{1}:{2}" -f $entry.InterfaceAlias, $entry.IPAddress, $Port)
    }
} else {
    Write-Host "- No non-virtual IPv4 addresses found on the Windows host."
}
Write-Host ""
Write-Host "Start the server inside WSL with host=0.0.0.0 and --allow-network."
Write-Host "Current portproxy table:"
& netsh interface portproxy show all
