$ErrorActionPreference = 'Stop'
$cfg = 'D:\25nov1-26july21\My project\sibees\SRIBEESonline\price-intel\backend\secrets.out\prod.env'
$kv = @{}
Get-Content $cfg | ForEach-Object { if ($_ -match '^(ONLINE_ADMIN_[A-Z]+)=(.*)$') { $kv[$matches[1]] = $matches[2].Trim('"') } }
$base = 'https://api.sribeesonline.com/api/v1'
$login = Invoke-RestMethod -Method Post -Uri "$base/admin/auth/login" -ContentType 'application/json' -Body (@{email=$kv['ONLINE_ADMIN_EMAIL']; password=$kv['ONLINE_ADMIN_PASSWORD']} | ConvertTo-Json)
$kv = $null
$tok = $login.data.token
$h = @{ Authorization = "Bearer $tok" }
Add-Type -AssemblyName System.Net.Http
$client = New-Object System.Net.Http.HttpClient
$client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', $tok)
$form = New-Object System.Net.Http.MultipartFormDataContent
$bytes = [System.IO.File]::ReadAllBytes("$PSScriptRoot\placeholder.jpg")
$fc = New-Object System.Net.Http.ByteArrayContent(,$bytes)
$fc.Headers.ContentType = 'image/jpeg'
$form.Add($fc, 'file', 'photo-coming-soon.jpg')
$resp = $client.PostAsync("$base/admin/products/upload-image", $form).Result
$txt = $resp.Content.ReadAsStringAsync().Result
if (-not $resp.IsSuccessStatusCode) { throw "upload failed $($resp.StatusCode)" }
$up = $txt | ConvertFrom-Json
$img = $up.data.image_url
if (-not $img) { $img = $up.data.url }
"image: $img"
$out = @()
foreach ($p in (Get-Content "$PSScriptRoot\new_products.json" -Raw | ConvertFrom-Json)) {
  $slug = ($p.name.ToLower() -replace "[^a-z0-9]+", '-').Trim('-')
  $body = [ordered]@{ name=$p.name; slug=$slug; sku=$p.sku; price=$p.price; category_id=$p.cat; subcategory_id=$p.sub;
    weight=$p.w; weight_unit='kg'; is_active=$true; images=@(@{image_url=$img; is_primary=$true; sort_order=0; alt_text=$p.name}) }
  if ($p.size) { $body.display_size = $p.size; $body.display_size_unit = $p.unit }
  try {
    $json = $body | ConvertTo-Json -Depth 5
    $r = Invoke-RestMethod -Method Post -Uri "$base/admin/products" -Headers $h -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
    $out += [pscustomobject]@{ key=$p.key; product_id=$r.data.product_id; name=$p.name }
    "$($p.key) created"
  } catch { "$($p.key) FAILED: $($_.ErrorDetails.Message)" }
}
$out | ConvertTo-Json | Set-Content -Encoding utf8 "$PSScriptRoot\created.json"
