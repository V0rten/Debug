# Rangkuman Perbaikan Timestamp Crash Game (Spaceman)

Dokumen ini merangkum masalah, temuan data, model, dan keputusan desain setelah debat panjang (Groq / Claude / Gemini / Qwen / GPT) dan uji pada file nyata (`raw1`, `raw2`, `raw3`, `master_merged`).

---

## 1. Masalah inti

Data diambil lewat WebSocket / scraper (Playwright). Hanya **satu tag Epoch dari server** yang andal. Sisanya (Ts Starttime, Ts Start Dealing, Ts Gr) bisa:

- delay karena lag / antrian pesan
- hilang karena reconnect, disconnect, AFK kick
- “berdempetan” (beberapa tag diterima di waktu yang hampir sama)
- History murni: tidak pernah di-capture live → tidak ada Epoch

**Tujuan praktis:** isi dan perbaiki timestamp yang bolong/rusak agar dataset utuh untuk proses lanjutan, dengan toleransi target ~**500 ms** di tempat yang masih bisa diverifikasi — tanpa mengarang kepastian di tempat yang tidak punya anchor.

---

## 2. Apa yang menjadi ground truth

| Sumber | Status |
|--------|--------|
| **Ts Epoch Starttime** (server) | Satu-satunya ground truth absolut |
| Result Gr (multiplier) | Tidak diubah; dipakai untuk model durasi |
| Id Game | Berurutan (+100), tidak duplikat, tidak gap |
| Ts Start Dealing / Ts Gr (client) | Observasi ber-noise; boleh dipakai jika lolos uji struktur, bukan kebenaran mutlak |
| Ts Starttime (Bukan Epoch) | Sering hanya resolusi **detik** → tidak sub-500 ms |

Tidak ada sinyal server untuk momen exact “Start Dealing” atau “Gr” per ronde. Deteksi kerusakan 1–2 detik di client **selalu probabilistik**.

---

## 3. Dua model yang tidak boleh dicampur

### 3.1 K_FLIGHT — pure flight (Dealing → Gr)

```
flight_ms ≈ (1000 / K_FLIGHT) * ln(mult)
K_FLIGHT ≈ 0.0781
```

Setara bentuk lain: `flight ≈ 12.804 * ln(mult)` (karena `1/0.0781 ≈ 12.804`). Bukan dua model bertentangan.

### 3.2 K_GAP — interval penuh (Epoch[n] → Epoch[n+1])

```
gap_ms ≈ BASE_GAP_MS + (1000 / K_GAP) * ln(mult)
K_GAP ≈ 0.0830
BASE_GAP_MS ≈ 10000–10050   (≈ fase betting + jeda post-crash di mult≈1)
```

**Epoch→Epoch = betting + flight + post-crash.**  
Pakai K_GAP sebagai “lama flight saja” = salah fase.

### 3.3 Bug satuan yang pernah muncul

Salah:

```text
step = base_gap_ms + ln(mult) / K_GAP
# ln/K hasilnya DETIK, dijumlah ke milidetik → error puluhan detik
```

Benar:

```text
step = base_gap_ms + (1000 / K_GAP) * ln(mult)
```

---

## 4. Temuan dari data (bukan teori)

### 4.1 Proporsi kerusakan WebSocket

Di sampel gabungan (~1400–4700 baris):

- Live ber-Epoch: ~83–91%
- **Rusak WS** (Epoch ada, dealing/gr missing atau delay outlier): **~13–16%** total baris
- History tanpa Epoch: ~9–16% (bukan “rusak WS”, memang tidak di-capture)

Kerusakan paling sering: **dealing delay outlier**, lalu Gr/Dealing missing.

### 4.2 Delay Epoch → Start Dealing

- Median stabil **~7000–7110 ms**
- Band robust (median ± 3.5×MAD) kira-kira **6000–8500 ms**
- Di luar band → treat sebagai reconnect/AFK → ganti ke median
- **Di dalam band** delay +1–2 s **tidak terdeteksi** sebagai rusak (batas metode)

### 4.3 Akurasi formula flight vs measured

| Range mult | Perilaku residual formula vs measured |
|------------|----------------------------------------|
| 1.01 – 1.5 | Sering masih mendekati target 500 ms |
| 3 – 10 | Sering di atas 500 ms |
| ≥ 50 | Error **detik**; measured **sistematis lebih pendek** dari formula (median bias ~−2–3 s) |
| 5000x (maxwin) | Contoh error ~6–7 s; sample sangat sedikit — **jangan overfit K** |

**Satu nilai K tidak lolos target 500 ms di seluruh range multiplier.**

### 4.4 Stabilitas flight setelah repair (uji master_merged, script GPT)

| Bin mult | Median flight | IQR / median | Catatan |
|----------|---------------|--------------|---------|
| 1.00–1.01 | ~0.72 s | **~41%** | paling berisik |
| 1.05–1.10 | ~1.4 s | ~33% | |
| ~50 | ~47.7 s | **~2.5%** | relatif paling rapat |
| 100+ | — | sample tipis | |

Median naik seiring mult (arah model benar).  
Di mult rendah, **inversi antar baris** sering terjadi (mult lebih tinggi bisa punya flight lebih pendek ≥0.5 s) karena noise client, bukan karena model gap Epoch salah.

### 4.5 Timezone / parse jam

Di `master_merged`, string waktu tampil sebagai **WITA (UTC+8)** wall clock, Epoch dalam **UTC ms**.  
Parse sebagai UTC murni → delay palsu ~8 jam.  
Pendekatan aman: clock label tanpa hardcode zona; pilih tanggal ±1 hari terdekat ke Epoch; offset dikalibrasi dari data.

---

## 5. Kebijakan perbaikan yang disepakati

| Kasus | Tindakan |
|-------|----------|
| Epoch valid | **Jangan diubah** |
| Result Gr | **Jangan diubah** |
| Dealing delay dalam band | Pertahankan (opsional: soft-flag jika ±1 s dari median) |
| Dealing outlier / kosong | `Epoch + DELAY_MEDIAN` |
| Gr measured, struktural valid | **Pertahankan** (jangan timpa formula di mult tinggi) |
| Gr kosong / tidak valid | (1) backward: `next_Epoch − post_gap_median` jika ada; (2) else `dealing + flight model` |
| Mult tinggi + formula Gr | Confidence **LOW** |
| History internal (ada 2 Epoch anchor) | Backfill proporsional / satu blok + catat uncertainty |
| History leading/trailing (satu/nihil anchor) | **Idealnya kosong**; jika diisi demi file penuh → label **extrapolation**, bukan server truth |

**“Tidak disentuh” ≠ “akurasi terjamin &lt;500 ms”.**  
Artinya hanya: tidak terbukti outlier kasar terhadap Epoch / urutan / band delay.

---

## 6. Target 500 ms — cara memakainya

- **Metrik audit**, bukan syarat mutlak untuk mengisi sel.
- Cocok dievaluasi pada residual **K_GAP** (Epoch→Epoch) di banyak bin.
- **Tidak** otomatis berlaku untuk residual **K_FLIGHT** (Dealing→Gr), terutama mult tinggi dan mult sangat rendah (noise besar).
- Klaim “96% residual ≤500 ms” harus menyebut **model mana** (gap vs flight) dan **sample per bin**.

---

## 7. Kesalahan yang harus ditolak

1. Interpolasi waktu dari **nomor baris** (`epoch = A + ratio * (B−A)` dengan ratio index).
2. Timpa **semua** Ts Gr dengan rumus “eksak”.
3. Klaim error 5000x = sub-millisecond tanpa data.
4. Menyamakan K_GAP dengan pure flight.
5. `base_ms + ln(mult)/K` (bug satuan detik vs ms).
6. Menganggap offset `epoch − HH:MM:SS` (resolusi detik) sebagai kalibrasi sub-500 ms yang ketat.
7. Menyebut hasil backfill History ujung sebagai ground truth server.

---

## 8. Angka kalibrasi tipikal (master_merged)

Nilai dihitung ulang per file; angka di bawah orde yang berulang:

```text
DELAY_MEDIAN (Epoch → Dealing) ≈ 7000–7110 ms
Dealing band (robust)          ≈ 6000–8500 ms
K_FLIGHT                       ≈ 0.0775–0.0782
K_GAP                          ≈ 0.0830
BASE_GAP                       ≈ 10000–10050 ms
Post-gap (Epoch[n+1] − Gr[n])  ≈ 2400 ms (median)
Id Game step                   = 100
```

---

## 9. Status script (ringkas)

| Versi | Kelebihan | Kekurangan utama |
|-------|-----------|------------------|
| Awal (fixed K, timpa Gr) | Sederhana | Timpa measured; kurang robust |
| Claude proses8-ish | Keep measured; hati-hati mult | Backfill terbatas |
| GPT `test_kalibrasi_reviewed` | Satuan benar; backward Gr; uncertainty | Klaim residual gap jangan disamakan flight |
| LLM `repair_ts (1)` | MD 8 kolom | Bug satuan K_GAP; leading diisi paksa |
| **`repair_ts_current`** | Satuan benar; keep struktural; MD bersih; one_sided dilabel | Tidak backward-Gr; soft delay tidak diflag di MD |
| **`repair_ts_grok`** | Soft-flag; tolak leading; a–d di header | Output lebih ke CSV+report |

Untuk **pipeline MD 8 kolom penuh**: `repair_ts_current` (atau setara) masuk akal, dengan syarat baris `one_sided` tidak diperlakukan sebagai Epoch server.

Untuk **audit ketat**: prefer script yang menolak leading/trailing, menyimpan raw columns, dan memisahkan residual gap vs flight per bin.

---

## 10. Checklist sebelum percaya hasil repair

- [ ] Epoch yang tadinya valid masih sama dengan sumber?
- [ ] Result Gr tidak berubah?
- [ ] Satuan model gap: `(1000/K)*ln` + base dalam ms?
- [ ] Berapa persen Gr masih measured vs diganti model?
- [ ] Blok History leading/trailing: diisi atau dikosongkan? Jika diisi, apakah berlabel extrapolation?
- [ ] Residual dilaporkan terpisah: K_GAP vs flight, per bin mult + jumlah sample?
- [ ] Timezone/parse jam: tidak menghasilkan delay ~8 jam palsu?

---

## 11. Kesimpulan satu paragraf

Hanya Epoch server yang ground truth. Kerusakan WebSocket nyata sekitar belasan persen baris Live (delay dealing outlier dan missing). Model log-growth berguna, tetapi **K_GAP dan K_FLIGHT beda fase** dan **satu K tidak akurat di semua multiplier**. Measured client yang lolos uji struktur lebih baik dipertahankan daripada ditimpa formula, terutama di mult tinggi. History tanpa dua anchor tidak bisa diisi dengan klaim ±500 ms; jika tetap diisi demi kelengkapan file, itu rekonstruksi berlabel, bukan data server. Target 500 ms adalah alat ukur kejujuran model, bukan stempel bahwa setiap sel di tabel sudah “benar”.

---

*Dokumen ini merangkum konsensus kerja perbaikan timestamp Spaceman berdasarkan data dan uji silang antar pendekatan LLM, bukan spesifikasi resmi game.*
