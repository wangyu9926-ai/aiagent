# Run this in an elevated PowerShell on the Windows host.
# Allows LAN clients to reach the portal through Caddy (80/443).
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script as Administrator."
    exit 1
}

# Windows often classifies a working LAN as "Public". Scope the opening to
# exactly these two web ports and include Public so LAN clients can connect.
$profiles = "Domain,Private,Public"
foreach ($port in 80, 443) {
    $name = "Hermes Portal TCP $port"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
            -LocalPort $port -Action Allow -Profile $profiles | Out-Null
        Write-Host "Added: $name ($profiles)"
    } else {
        Set-NetFirewallRule -DisplayName $name -Enabled True -Action Allow -Profile $profiles | Out-Null
        Write-Host "Updated: $name ($profiles)"
    }
}

Write-Host "LAN access rules ready. Use https://<this-host-LAN-IP>/ from colleague PCs."
