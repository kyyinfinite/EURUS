# Google Workspace MCP untuk Eurus — Panduan Setup

Panduan ini menjelaskan cara menghubungkan Eurus ke Google Workspace lewat
MCP (Model Context Protocol) resmi Google: Gmail, Drive, Docs, Sheets,
Slides, Calendar, Chat, People, dan Classroom.

> Ringkasan dalam bahasa Inggris ada di bagian paling bawah
> ([English summary](#english-summary)).

---

## 1. Ringkasan cara kerja

- Eurus adalah **klien MCP**. Google menjalankan **server MCP resmi**
  (Streamable HTTP, endpoint `https://<service>mcp.googleapis.com/mcp/v1`)
  yang masih berstatus **Developer Preview** — ketersediaannya bisa berubah.
- Autentikasi memakai **OAuth 2.0** milik Anda sendiri: Eurus **tidak
  menyertakan** client ID/secret bawaan. Anda membuat OAuth client di Google
  Cloud Console, lalu Eurus memakainya.
- Token akses tidak pernah dikirim ke host selain `*.googleapis.com`, dan
  hanya ke server yang namanya **dan** URL-nya persis cocok dengan katalog
  resmi di Eurus.

## 2. Prasyarat

1. Akun Google (personal, Workspace, atau Education — tergantung layanan).
2. Sebuah **project Google Cloud** (gratis; aktifkan di
   <https://console.cloud.google.com>).
3. Python 3.11+ dan Eurus terpasang. Untuk penyimpanan token terenkripsi,
   pasang ekstra `[google]`: `pip install "eurus-agent[google]"`.
4. Jika memakai Termux/SSH tanpa browser, login tetap bisa lewat opsi
   `--paste` (lihat bagian 7).

## 3. Aktifkan API dan layanan MCP

Setiap layanan butuh **dua** hal di project Google Cloud: *product API* dan
*service MCP*-nya. Cara tercepat lewat `gcloud`:

| Layanan | gcloud services enable |
| --- | --- |
| Gmail | `gmail.googleapis.com` + `gmailmcp.googleapis.com` |
| Drive | `drive.googleapis.com` + `drivemcp.googleapis.com` |
| Docs | `docs.googleapis.com` + `docsmcp.googleapis.com` |
| Sheets | `sheets.googleapis.com` + `sheetsmcp.googleapis.com` |
| Slides | `slides.googleapis.com` + `slidesmcp.googleapis.com` |
| Calendar | `calendar-json.googleapis.com` + `calendarmcp.googleapis.com` |
| Chat | `chat.googleapis.com` + `chatmcp.googleapis.com` |
| People | `people.googleapis.com` |
| Classroom | `classroom.googleapis.com` + `classroommcp.googleapis.com` |

Contoh untuk Gmail + Drive:

```bash
gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com \
                       drive.googleapis.com drivemcp.googleapis.com
```

Link konsol per layanan: <https://console.cloud.google.com/apis/library>.

Catatan **Google Chat**: tool *baca* hanya butuh Chat API + OAuth client.
Tool *tulis* tambahan butuh **Chat app** yang dikonfigurasi di project.
Akun Google personal (non-Workspace) umumnya tidak bisa memakai Chat.

## 4. Buat OAuth client (WAJIB centang "agentic")

1. Buka **APIs & Services → OAuth consent screen**. Pilih tipe:
   - **Internal** (hanya Google Workspace org) — token tidak kedaluwarsa.
   - **External** — publikasikan ke **Production** agar token tidak
     kedaluwarsa; dalam mode **Testing**, refresh token kedaluwarsa
     **7 hari** dan Eurus akan mengingatkan Anda mendekati tenggat.
2. Tambahkan akun Anda sebagai **test user** jika masih Testing.
3. Buka **Credentials → Create credentials → OAuth client ID**:
   - Tipe **Desktop app** (direkomendasikan; redirect loopback dinamis,
     tanpa URI tetap), atau
   - Tipe **Web application** dengan redirect URI
     `http://127.0.0.1:<port>/callback` (port dikonfigurasi lewat
     `oauth.redirect_port` di config, lihat bagian 8).
4. **PENTING (klien agentic):** saat membuat client, aktifkan opsi
   **"Use this client for an AI-powered agent"**. Mulai **11 November 2026**
   Google menolak client OAuth yang tidak ditandai demikian, dan mulai
   **12 November 2026** client lama **tidak bisa diubah** — Anda harus
   membuat client BARU.
5. Unduh JSON client-nya, lalu impor ke Eurus:

```bash
eurus google setup --client-file ~/Downloads/client_secret_xxx.json
```

Atau set env var: `EURUS_GOOGLE_CLIENT_ID` / `EURUS_GOOGLE_CLIENT_SECRET`.

## 5. Aktifkan layanan di Eurus

```bash
eurus mcp google list                 # lihat katalog 9 layanan
eurus mcp google enable gmail         # satu per satu
eurus mcp google enable drive
eurus google setup --preset student   # atau pakai bundle preset siap pakai
```

Setiap layanan yang diaktifkan menambahkan scope OAuth saat login berikutnya.
Eurus mendeteksi *scope drift* (layanan diaktifkan setelah login) dan akan
meminta login ulang.

## 6. Login

```bash
eurus auth google login               # buka browser otomatis
eurus auth google login --no-browser  # cetak URL, browser di perangkat lain
eurus auth google login --paste       # fallback tempel URL (headless)
```

Alur normal: browser membuka URL konsent → Google mengarahkan ke
`http://127.0.0.1:<port>/callback` → Eurus menukar kode menjadi token.
Koneksi browser yang "mengintip" (preconnect) **tidak** lagi membuat login
gagal; server callback Eurus hanya menanggapi `/callback` yang valid.

Status & kelola akun:

```bash
eurus auth google status    # umur token, status, peringatan kedaluwarsa
eurus auth google accounts
eurus auth google use <email-atau-id>
eurus auth google logout [akun | --all]
```

## 7. Termux / SSH / tanpa browser

1. Jalankan `eurus auth google login --no-browser --paste`.
2. Buka URL yang dicetak di browser mana pun (mis. laptop atau ponsel).
3. Setelah konsent, salin **URL lengkap** dari address bar browser
   (`http://127.0.0.1:.../callback?code=...&state=...`) — atau hanya nilai
   `code` — lalu tempel ke prompt Eurus.

Mode `--paste` juga aktif sebagai fallback bila callback timeout dan Anda
menjalankan `eurus google setup --login --paste`.

## 8. Config v2 (`integrations/google-workspace.json`)

```json
{
  "version": 2,
  "preset": "personal",
  "account": "auto",
  "access": "standard",
  "startup": "background",
  "timeout": 45,
  "oauth": { "publish_status": "testing", "redirect_port": 0 },
  "tools": { "deny": ["delete_*"] },
  "services": {
    "gmail": { "enabled": true, "timeout_seconds": 45 }
  }
}
```

- `preset`: `personal` | `student` | `workspace` | `custom` — memilih bundle
  layanan: `personal` = gmail, drive, calendar, people; `student` = gmail,
  drive, docs, sheets, slides, calendar, classroom; `workspace` = gmail,
  drive, docs, sheets, slides, calendar, chat, people; `custom` = pilihan
  Anda sendiri. Terapkan dengan `eurus google setup --preset <nama>` atau
  aktifkan layanan satu per satu lewat `eurus mcp google enable`.
- `account`: `auto` (ikut akun aktif) atau pin ke satu akun (id/email).
  Jika akun pin tidak ditemukan, Eurus melaporkan perlu login — tidak pernah
  diam-diam memakai token akun lain.
- `access` — lantai kebijakan risiko untuk tool Google:
  - `read`: semua tool yang menulis **wajib approval** per panggilan.
  - `standard`: sesuai klasifikasi bawaan (default).
  - `full`: penulisan reversible tanpa approval; sensitive/destructive tetap
    wajib approval.
- `startup`: cara koneksi saat mulai:
  - `background` (default): connect di belakang; giliran pertama menunggu
    maksimal ~12 detik lalu lanjut tanpa tool sampai server siap.
  - `eager`: menunggu sampai koneksi tuntas (batas budget koneksi penuh)
    sebelum giliran pertama dieksekusi.
  - `lazy`: tidak connect saat start; koneksi baru dimulai saat tool
    pertama benar-benar dibutuhkan.
- `oauth.publish_status`: `testing` | `production` | `internal` — dipakai
  untuk peringatan proaktif kedaluwarsa token 7 hari.
- `tools.deny`: pola fnmatch; tool yang cocok **tidak didaftarkan** sehingga
  tidak bisa dipanggil model. Pola boleh menempel pada nama penuh
  (`mcp_google_gmail_delete_*`), nama per layanan (`gmail_delete_*`), atau
  nama tool-nya saja (`delete_*`). Hanya tool Google yang terpengaruh.
- `services.<id>.timeout_seconds` menimpa `timeout` global (per-service
  override).
- File versi lama (v1) otomatis dimigrasi saat disimpan — tidak ada data
  yang hilang. Key tidak dikenal ditolak dengan pesan yang menunjukkan
  key-nya.
- File JSON yang korup **tidak** ditimpa diam-diam: dibackup sebagai
  `<nama>.corrupt-<timestamp>` lalu dipakai nilai default + peringatan.

## 9. Kebijakan risiko (`security/mcp-policies.json`)

Klasifikasi risiko tiap tool diputuskan dengan urutan:

1. **Aturan eksplisit** Anda (paling tinggi) — `mcp permissions --set`.
2. **Anotasi tool** dari server (`readOnlyHint`/`destructiveHint`).
3. **Heuristik nama** verb-first untuk server `google-*` (fallback).
4. Sisanya `unknown` → fail-closed (wajib approval).

Contoh: `list_shared_drives` tetap read-only (tidak kena kata "share"),
`search_and_*` dianggap baca; tool penghapusan selalu wajib approval.

## 10. Verifikasi

```bash
eurus google doctor      # connect nyata per layanan + kode error + solusi
eurus mcp doctor         # seluruh stack MCP (SDK, server, policy, Google)
eurus mcp google test    # tabel state/tool per layanan
```

Di TUI, ketik `/mcp` kapan pun untuk status server MCP + Google Workspace.

## 11. Pemecahan masalah

| Kode | Arti | Solusi |
| --- | --- | --- |
| `agentic_client_required` | OAuth client tidak dicentang "AI-powered agent" | Buat **client BARU** dengan opsi itu (client lama tak bisa diubah setelah 11 Nov 2026) |
| `service_disabled` | API/layanan MCP belum diaktifkan di project | `gcloud services enable ...` (lihat bagian 3; pesan error memuat nama layanannya) |
| `scope_missing` | Token tidak punya scope yang dibutuhkan | `eurus auth google login` ulang |
| `token_expired` | Refresh token kedaluwarsa/dicabut | `eurus auth google login`; jika consent screen **Testing**, token mati tiap 7 hari — publikasikan ke Production |
| `access_denied_test_user` | Akun bukan test user | Tambahkan akun di OAuth consent screen, atau publish |
| `redirect_mismatch` | Redirect URI tidak terdaftar | Pakai client Desktop, atau daftarkan `http://127.0.0.1:<port>/callback` |
| `client_not_configured` | `invalid_client` — client ID/secret salah | Periksa `client.json` / env var |
| `store_unavailable` | Keyring terkunci / passphrase salah | Buka keyring OS atau set `EURUS_GOOGLE_TOKEN_PASSPHRASE`; **jangan** re-login |
| `preview_not_available` | Akun/project belum dapat akses Preview | Ikuti program Developer Preview / coba akun lain |
| `transient` / `network` | 429/5xx/jaringan | Otomatis di-retry; tidak perlu tindakan |

Lain-lain:

- Gmail MCP (preview) hanya menyediakan tool terbatas: `create_draft`,
  `get_message`, `get_thread`, `label_message`, `label_thread`,
  `list_drafts`, `list_labels`, `search_threads`, `unlabel_message`,
  `unlabel_thread` — **tidak ada** kirim/hapus email.
- Token tidak pernah dikirim ke server non-Google; percobaan redirect lintas
  host ditolak.

## English summary

Eurus connects to Google's official remote Workspace MCP servers
(Streamable HTTP, `https://<service>mcp.googleapis.com/mcp/v1`) using your
own OAuth 2.0 client. Setup: enable both the product API and the MCP service
per app in your Google Cloud project, create a **Desktop** OAuth client with
**"Use this client for an AI-powered agent"** checked (mandatory from
2026-11-11; old clients can't be changed after 2026-11-12), import it with
`eurus google setup --client-file <json>`, enable services with
`eurus mcp google enable <service>`, then `eurus auth google login` (use
`--no-browser --paste` on headless/Termux setups). Consent screen **Testing**
mode expires refresh tokens after **7 days** — publish to Production (or use
Workspace Internal) for long-lived tokens. Config lives in
`integrations/google-workspace.json` (v2, see section 8; v1 files migrate
automatically). Diagnose with `eurus google doctor` / `eurus mcp doctor` /
`/mcp` in the TUI. Tokens are only ever sent to `*.googleapis.com` endpoints
that exactly match Eurus' service catalog.
