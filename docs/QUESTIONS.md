# Sribees Supply — ප්‍රශ්න (owner ට)

2026-11-12 ඔබ ගමනක් ගිය වෙලාවේ ලියපු ප්‍රශ්න. එක එක ප්‍රශ්නයට මගේ යෝජනාවත් තියෙනවා.
ඔබ ආවම මේ ප්‍රශ්න MCQ විදියට අහන්නම්.

## A. වෙනත් repo / system වෙනස් කිරීම් (ඔබේ අවසරය ඕන)

1. **Online එකේ අලුත් internal endpoint එකක්: `/internal/supply/catalog`.**
   Supply එකට Online products ලැයිස්තුව ගන්න ඕන: සිංහල නම (`name_si`), size සහ weight variants.
   Finance එකේ තියෙන `/internal/finance/catalog` එකේ මේ දත්ත නෑ.
   Online `development` branch එකේ මේ endpoint එක හදන්නද?
   *යෝජනාව: ඔව්. Finance එකට කළ විදියටම token hash එකක් සහ internal network එක විතරයි.*
2. **Online products වල සිංහල නම (`name_si`) කීයක පුරවලා තියෙනවද?**
   Matching එක හොඳම විදියට වැඩ කරන්නේ සිංහල නම තියෙනකොට.
   Prod DB එකෙන් **ගණන විතරක්** බලන්නද (read-only)?
3. **Finance සහ Price Intel වල X-Forwarded-For ප්‍රශ්නය.**
   ඒ system දෙකම user ගේ IP එක ගන්නේ header එකේ මුල් අගයෙන්. ඒ අගය බොරුවට දාන්න පුළුවන්, ඒ නිසා audit log එකේ IP වැරදි වෙන්න පුළුවන්.
   ඒ repo දෙකේ fix කරන්නද?
   *යෝජනාව: ඔව්. මේක පේළි දෙකක වෙනසක්, අවදානම අඩුයි.*
4. **Supplier ගේ OTP SMS එක යවන්නේ කොහෙන්ද?**
   Online එක පාවිච්චි කරන Text.lk account එකෙන්ම, ඒ sender ID එකෙන්ම යවන්නද?
   (Test SMS කවදාවත් යවන්නේ නෑ.)

## B. Deploy / repo

5. **GitHub එකට push කරන්නද?**
   Local monorepo `SRIBEESonline/supply` එකේ `backend/` කොටස `Sribees-Online-Base-Price-Backend` repo එකේ `development` branch එකට push කරන්නද?
   Finance එකේ වගේම subtree split එකක් හරහා push කරනවා.
   *යෝජනාව: development එකට විතරක් ඔව්. main එකට push කරන්න කලින් ආයේ අහනවා.*
6. **Dev server එක.**
   Contabo එකේ `/opt/sribees-supply-dev` folder එකට deploy කරලා, `devsupply.sribees.com` domain එක හදන්නද?
   DNS A record එකයි Caddy block එකයි ඕන.
   Caddy block එක තියෙන්නේ Express repo එකේ, ඒක Express main එකට push කරන්න ඕන. ඒ නිසා ඒකටත් ඔබේ අවසරය ඕන.

## C. Business

7. **Nishi ගේ ගිනි පෙට්ටි පේළිය: `(8490)(14400)(720)`.**
   මේකේ තේරුම මොකක්ද? 8490 කියන්නේ box එකක මිලද? 14400 කියන්නේ MRP එකද? 720 කියන්නේ box එකක තියෙන පෙට්ටි ගණනද?
   Nishi ගෙන් අහලා කියන්න.
8. **ගන්න මිල MRP එකට වඩා වැඩි නම්?**
   දැනට system එක එහෙම පේළියක් flag කරනවා, supplier ට ඒක skip කරන්න විතරයි පුළුවන්.
   ඒ වගේ මිලක් confirm කරන්න ඉඩ දෙන්නද?
9. **Order approval එක කාගෙද? (Finance එක තවම live නෑ)**
   Plan එකේ තියෙන්නේ Finance PO approval rule එක පාවිච්චි කරන්න කියලා (රු. 100,000ට වැඩි නම් owner).
   Finance live වෙනකම් Supply එකේදීම owner approve කරන්නද?
   *යෝජනාව: ඔව්, Finance live වෙනකම්. ඊට පස්සේ setting එකක් මාරු කරලා Finance එකට දෙනවා.*
10. **Order නොගැලපුණු offer එකක් order කරන්න ඉඩ දෙනවද?**
    ඒ කියන්නේ Online product එකකට match කරලා නැති supplier offer එකක්.
    *යෝජනාව: නෑ. Match එකක් නැත්නම් stock එක ලැබුණාම ඒක යන්න ඕන Online product එක දන්නේ නෑ, base price එකත් ලියන්න බෑ.*
11. **Supplier ට app notification (FCM) එක.**
    ඒකට අලුත් Firebase Android app එකක් හදන්න ඕන (package එකක් සහ SHA fingerprint එකක්).
    දැනට තියෙන Sribees Firebase project එකේම හදන්නද?
12. **Online එකේ weight variants (250g / 500g / 1kg).**
    Supplier offer එකක් match කරන්නේ product එකටද, variant (pack size) එකටද?
    *යෝජනාව: දෙකම හැකි වෙන්න හැදුවා. Pre-packed බඩු variant එකට match කරනවා.*

## D. ආරක්ෂක review එකෙන් ඉතුරු වූ කරුණු (2026-11-12)

වෙනම reviewer කෙනෙක් (නැවුම් context එකකින්) Supply backend එක review කළා. HIGH එක සහ MEDIUM තුනම fix කළා, ඒ හැම එකකටම test එකකුත් තියෙනවා. ඉතුරු කරුණු මේවා:

13. **කවුරුහරි supplier ගේ අංකයට ආයේ ආයේ code ඉල්ලලා, ඔවුන්ට login වෙන්න බැරි කරන්න පුළුවන්.**
    මේක දැන් ගොඩක් අමාරුයි. එක IP එකකින් පැයට code 5ක් විතරයි, අංකයකට මුළු පැයටම 10ක් විතරයි, අන්තිම code 3ම වැඩ කරනවා.
    හැබැයි ප්‍රශ්නය සම්පූර්ණයෙන්ම නැති කරන්න Google Play Integrity (app attestation) ඕන.
    *යෝජනාව: Play Store එකට දාන වෙලාවේ Play Integrity එකතු කරන්න.*
14. **Order එක cancel කරද්දී ලියන හේතුව supplier ට පේනවා.**
    ඒ නිසා staff ලා ඇතුළත් කාරණා හේතුවට ලියන්න හොඳ නෑ.
    *යෝජනාව: supplier ට පේන හේතුවයි, අපිට විතරක් පේන සටහනයි කියලා field දෙකකට වෙන් කරන්න.*
15. **Caddy ට ඉස්සරහින් Cloudflare වගේ CDN එකක් දැම්මොත්, per-IP limit එක වෙනස් කරන්න ඕන.**
    දැනට IP එක ගන්නේ Caddy එකතු කරන අවසාන අගයෙන්. CDN එකක් දැම්මොත් ඒ අගය CDN එකේ IP එක වෙනවා, ඒ නිසා ඔක්කොම users ලා එක IP එකක් වගේ පේනවා.

---

## Owner ගේ උත්තර (2026-11-12, MCQ)

| # | තීරණය |
|---|---|
| 1 | Online `development` එකේ `/internal/supply/catalog` endpoint එක හදන්න: **ඔව්** |
| 2 | Prod DB එකෙන් `name_si` ගණන බලන්න (read-only): **ඔව්** |
| 3 | Finance + Price Intel XFF fix (development): **ඔව්** |
| 4 | SMS: Online එකේ Text.lk account එකෙන්ම: **ඔව්** (dev = fixed code, test SMS කවදාවත් නෑ) |
| 5 | 810 repo වල `development` branch එකට push: **ඔව්** (main නෑ) |
| 6 | Dev server `devsupply.sribees.com` + Caddy (Express main): **ඔව්** |
| 7 | Nishi ගිනි පෙට්ටි පේළිය: **owner Nishi ගෙන් අහනවා** |
| 8 | Price > MRP: **නවත්වන්න; අපිට label එකක්.** ඒ පේළිය message එකට යන්නේ නෑ, order කරන්නත් බෑ. Supplier හරි කළාම label එක යනවා. |
| 9 | Approval: **Finance එකයි Supply එකයි එකටම live කරන්න.** Go-live වෙද්දී approval එක Finance එකෙන් (setting එක `finance`). ඊට කලින් Supply → Finance PO link එක හදන්න ඕන. Dev එකේ owner approve කරනවා. |
| 10 | Match නැති offer එකක් order කරන්න: **ඉඩ දෙන්න.** Base price එක ලියවෙන්නේ receiving එකේදී match කළාම. |
| 11 | FCM notification: **දැනට තියෙන Sribees Firebase project එකේ** |
| 12 | Variants: product එකටත් variant එකටත් match කරන්න පුළුවන් විදියට හැදුවා (ඒ ගැන අහලා නෑ) |
| 14 | Cancel හේතුව: **field දෙකක්** (supplier ට පේන හේතුව / අපිට විතරක් පේන සටහන) |

## 2026-11-26 රෑ වැඩ — උදේට owner ගෙන් අහන්න

| # | ප්‍රශ්නය / අවහිරය | මම කළේ |
|---|---|---|
| N1 | Staff app repo `anjanamadushan810/Sribees-Online-Supply-Staff-App` හැදුවා, හැබැයි 810 token එකට ඒකට ලියන්න අවසර නෑ (403). Public repo හදන්න auto-mode එක ඉඩ දුන්නේ නෑ, ඒ නිසා **private**. | GitHub → Settings → Developer settings → token → Repository access එකට මේ repo එක එකතු කරන්න (Contents + Actions + Secrets + Variables: read/write). එතකොට මම push කරලා APK එක build කරනවා. Public කරන්න ඕනද කියලත් කියන්න. |
| N2 | Invoice photo: backend එකේ photo upload එකක් තාම නෑ. | Staff app එකේ invoice අංකය + මුළු මුදල විතරයි. Photo එක ඊළඟ වටයේ. |
| N3 | Finance Quick buy (ඊයේ server එකේ හදපු) Finance එකේ බඩු ලබාගන්නවා. Decision 21 අනුව මිලදී ගැනීම් ඔක්කොම Supply එකෙන් යන්න ඕනේ. | Plan step 8: Quick buy → Supply "cash buy". |
| N4 | Return කරපු බඩු කෙලින්ම ආපහු විකුණන්න දාන්නද, නැත්නම් Finance එකේ කලින් නීතිය වගේ බලනකම් quarantine කරන්නද? | දැනට කෙලින්ම batch එකට. |
| N5 | Go-live දවසේ Finance worker එක `WORKER_SUPPLY_ONLY` කරන්න ඕනේ. නැත්නම් Supply එකෙන් යන orders වල වියදම දෙපාරක් ලියවෙනවා. | Go-live checklist එකට දැම්මා. |
| N6 | Dev එකේ pack scan test කරන්න Online dev එකයි Supply dev එකයි අතරේ token එකක් ඕනේ (Online deploy-dev secrets). | ඔබ OK කිව්වොත් මම හදනවා. |

### Owner ගේ උත්තර (2026-11-26 උදේ)
| # | තීරණය |
|---|---|
| N1 | Staff app repo **310 account එකේ**: `anjanamadushan310/Sribees-Online-Supply-Staff-App` (private), development push කළා, STAFF_KEYSTORE_* secrets + SUPPLY_API_URL(_PROD) variables දැම්මා. හිස් 810 repo එක තියෙනවා (මකන්නේ owner කිව්වොත් විතරයි). |
| N4 | Return කරපු බඩු **කෙලින්ම** ආපහු විකුණන්න දානවා (quarantine නෑ). |
| N5 | Go-live දවසේ Finance worker **WORKER_SUPPLY_ONLY** කරනවා: ඔව්. |
| N6 | Dev එකේ pack scan token: **ඔව්**. Online deploy-dev එකට DEV_SUPPLY_PACK_SCAN_ENABLED / _API_URL / _PICK_TOKEN, Supply dev.env එකට ONLINE_PICK_TOKEN_SHA256. |
