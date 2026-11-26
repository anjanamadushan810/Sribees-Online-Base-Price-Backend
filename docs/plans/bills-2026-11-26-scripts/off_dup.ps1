$ErrorActionPreference = 'Stop'
$cfg = 'D:\25nov1-26july21\My project\sibees\SRIBEESonline\price-intel\backend\secrets.out\prod.env'
$kv = @{}
Get-Content $cfg | ForEach-Object { if ($_ -match '^(ONLINE_ADMIN_[A-Z]+)=(.*)$') { $kv[$matches[1]] = $matches[2].Trim('"') } }
$base = 'https://api.sribeesonline.com/api/v1'
$login = Invoke-RestMethod -Method Post -Uri "$base/admin/auth/login" -ContentType 'application/json' -Body (@{email=$kv['ONLINE_ADMIN_EMAIL']; password=$kv['ONLINE_ADMIN_PASSWORD']} | ConvertTo-Json)
$kv = $null
$tok = $login.data.token
$h = @{ Authorization = "Bearer $tok" }
$id = '6de9fcb5-0b24-4dcd-8f9d-9720dc08c537'
$r = Invoke-RestMethod -Method Put -Uri "$base/admin/products/$id" -Headers $h -ContentType 'application/json' -Body (@{is_active=$false} | ConvertTo-Json)
"is_active now: $($r.data.is_active) ($($r.data.name))"
