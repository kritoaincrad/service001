# Service001

[English](README.md) | Türkçe

Claude Code token tüketimini ölçen ve azaltan araç seti.
Ürün vizyonu, ölçüm bulguları ve fazlar için [ROADMAP.md](ROADMAP.md) dosyasına bakın.

## Kurulum

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"     # Linux / macOS
```

## Claude Code token analizi

Claude Code her oturumu `~/.claude/projects` altında kaydeder.
Analiz bu kayıtları yerelde okur, hiçbir şeyi dışarı göndermez ve kayıtları değiştirmez.

```bash
.venv/Scripts/cc-optimizer analyze                  # tüm projeler
.venv/Scripts/cc-optimizer analyze --project .      # yalnızca bulunduğunuz klasörün projesi
.venv/Scripts/cc-optimizer analyze --days 7         # son 7 günde aktif oturumlar
.venv/Scripts/cc-optimizer analyze --session 3a54   # tek oturum, kimliğin başıyla
.venv/Scripts/cc-optimizer analyze --json           # makine okunur çıktı
```

Rapor bölümleri:

1. **Token tüketimi:** Cache okuma, cache yazma, girdi ve çıktı, model bazında.
2. **Bağlam büyüklüğü:** Çağrı başına bağlam, açılıştaki sabit yük, sıkıştırmalar ve daha erken sıkıştırma senaryosu.
3. **Bağlamı ne dolduruyor:** Her kaynağın boyutu ve kaç çağrı boyunca taşındığı. Modelin yazdığı içeriğin kırılımı.
4. **En pahalı araç çıktıları:** Bağlama en çok yük bindiren tek tek okumalar ve komutlar.
5. **Kabuk komutları:** Program ve alt komut bazında çıktı yükü.
6. **Dosya okumaları:** Gereksiz tekrar okumalar, sınırsız büyük okumalar, en çok okunan dosyalar.
7. **En çok tüketen oturumlar.**

### Ölçümün sınırları

- Model çağrılarının token sayıları kayıttaki gerçek değerlerdir.
- Kaynak bazındaki boyutlar `o200k_base` ile tahmin edilir. Sonra gerçek bağlam büyümesine göre kalibre edilir. Rapor, gerçek tüketimin ne kadarını açıklayabildiğini gösterir.
- Görseller sabit 1.500 token sayılır.
- Düşünme token'larının sonraki çağrılarda bağlamda kalmadığı varsayılır.
- Sıkıştırma senaryosu kaba bir tahmindir. Sıkıştırmadan sonra işin aynı şekilde ilerleyeceğini varsayar.
- Kayıt formatı Claude Code'un iç formatıdır ve sürümle değişebilir.

## Bağlam koruyucuları

Claude Code hook'ları ile çalışan, bağlamı şişiren davranışları yakalayan koruyucular.
İzin vermez, yalnızca Claude'a ve size bilgi verir. Tek istisna büyük okumalardır: orada bir kez reddetmek token kazandırır.

| Koruyucu | Ne zaman | Ne yapar |
|---|---|---|
| Oturum kuralları | Oturum başı, `/clear` ve sıkıştırma sonrası | Claude'a üç satırlık token kuralı ekler |
| Bağlam uyarısı | Bağlam 300 bin token'ı aşınca, sonra her 100 binde bir | Size `/compact` veya `/clear` önerir, Claude'a işi odaklı tutmasını söyler |
| Baştan yazma uyarısı | Var olan büyük bir dosya Write ile içeriğinin çoğu aynı kalarak yeniden yazılınca | Claude'a sonraki değişikliklerde yalnızca değişen satırları düzenlemesini söyler |
| Heredoc uyarısı | Aynı durum `cat > dosya <<EOF` ile yapılınca | Aynı uyarı |
| Büyük okuma | Yaklaşık 6 bin token'dan büyük bir dosya aralık verilmeden okunmak istenince | Bir kez reddeder, dosyanın satır numaralı taslağını verir ve Grep ile yer bulup aralık okumasını söyler. Claude aynı okumayı tekrarlarsa o oturumda o dosya için izin verilir |
| Kabukta dosya basma | `cat a.tsx b.tsx` veya `Get-Content` ile toplamı eşiği aşan dosyalar tam basılmak istenince | Büyük okumadaki gibi bir kez reddeder. Aralıklı basmalara (`sed -n`, `head`), başka komuta aktarılan ve dosyaya yönlendirilen çıktılara dokunmaz |
| Durum satırı | Her zaman | Model, bağlam büyüklüğü ve oturum maliyeti, eşiğe göre renkli |

Kurulum ve kaldırma:

```bash
.venv/Scripts/cc-optimizer install --scope local --statusline        # yalnızca bu proje
.venv/Scripts/cc-optimizer install --scope user --statusline         # tüm projeler
.venv/Scripts/cc-optimizer install --scope user --compact-window 400000   # sıkıştırma eşiği de
.venv/Scripts/cc-optimizer install --scope user --bash-output-max 12000   # uzun kabuk çıktısı dosyaya
.venv/Scripts/cc-optimizer install --scope user --dry-run            # yazmadan önce göster
.venv/Scripts/cc-optimizer uninstall --scope user                    # her şeyi eski haline getirir
```

Kurulum mevcut ayarları korur, önce yedek alır ve kaldırıldığında değiştirdiği değerleri geri yükler.

Koruyucular hata verdiğinde Claude Code'u bozmamak için sessizce devre dışı kalır. Bu yüzden ara sıra kontrol edin:

```bash
.venv/Scripts/cc-optimizer doctor
```

Kontrol ettikleri:
- Hook'lar kurulu ve güncel mi.
- Python yolu geçerli mi. Hook ve durum satırı gerçekten çalıştırılıp süreleri ölçülür.
- Koruyucular Claude Code kullanılırken gerçekten çalışmış mı. Her hook çalıştığında `~/.claude/cc-optimizer/heartbeat/` altına zaman bırakır.
- `config.json` geçerli mi.
- Alt ajanlar güncel mi.
- Bu projede ikinci bir kurulum var mı.
- Proje haritası yerinde mi.

Sorun varsa nasıl düzeltileceğini yazar ve hata durumunda 1 ile çıkar.

Ayarlar `~/.claude/cc-optimizer/config.json` dosyasından değiştirilebilir:

```json
{
  "context_warn_tokens": 300000,
  "context_step_tokens": 100000,
  "write_guard": "coach",
  "write_min_lines": 120,
  "write_unchanged_ratio": 0.6,
  "heredoc_guard": true,
  "read_guard": "block",
  "read_guard_tokens": 6000,
  "session_guidance": true
}
```

`write_guard` için `"block"` seçilirse baştan yazma bir kez reddedilir. Claude aynı yazmayı tekrar denerse izin verilir.
Bu mod varsayılan değildir, çünkü reddedilen içerik zaten üretilmiş ve bağlama girmiş olur.

`read_guard` için durum tersidir. Okuma reddedildiğinde dosya henüz bağlama girmemiştir.
İlk ölçümde 6 bin token'ı aşan 71 sınırsız okuma, sonraki çağrılarda toplam 223 milyon token taşıdı.
Reddetmenin getirdiği fazladan çağrılar 22 milyon token tuttu.
`"coach"` yalnızca uyarır, `"off"` kapatır. Aynı ayar kabukta dosya basmayı da yönetir.

Uzun kabuk çıktıları için ayrı bir koruyucu yok, çünkü Claude Code bunu kendisi yapıyor.
`bashOutputMaxChars` aşılınca çıktı bir dosyaya yazılır ve Claude yalnızca önizleme ile dosya yolunu görür.
Varsayılan sınır yaklaşık 30 bin karakterdir. `--bash-output-max` bunu düşürür ve kaldırınca eski haline getirir.

Koruyucuların etkisini ölçmek için:

```bash
.venv/Scripts/cc-optimizer events --days 7      # koruyucular ne yaptı
.venv/Scripts/cc-optimizer compare              # başlangıç ölçümüyle önce / sonra karşılaştırması
```

`compare`, `data/` içindeki en yeni başlangıç ölçümünü, ondan sonra başlayan oturumlarla karşılaştırır.
İki dönemdeki işler farklı olduğu için toplamları değil, çağrı başına değerleri, medyanları ve payları karşılaştırır.
Asıl ölçüt çağrı başına işlenen token'dır. Sonraki dönemde 5 oturum veya 1.000 çağrıdan az veri varsa uyarı verir.

## Çalışma günlüğü (proje hafızası)

Yeni bir sohbette veya bağlam sıkıştırıldıktan sonra Claude önceki işleri bilmez.
Service001 her proje için `.claude/worklog.md` dosyasında bir çalışma günlüğü tutar. Her oturum için şunları kaydeder:
- İstekleriniz.
- Değişen proje dosyaları ve her birinin kaç kez düzenlendiği.
- Çalışan komutlar. Okuma komutları hariç.
- Claude'un son cevabı.

Kayıtlar oturum kayıtlarından kural tabanlı olarak çıkarılır, yani ek token harcanmaz.
Yapıştırılmış içerikler, IDE notları ve proje dışındaki dosyalar ayıklanır. En yeni 40 oturum tutulur.

Bağlama ne girer:

| Ne zaman | Bağlama giren |
|---|---|
| Yeni sohbet | Günlüğün yolu ve son 3 oturumun tek satırlık özeti (tarih, başlık, değişen dosyalar), yaklaşık 150 token |
| Bağlam sıkıştırıldıktan sonra | Aynısı, ve bu oturumda sıkıştırmaya kadar yapılanların tek satırlık özeti |

Claude önceki bir işe devam ederken günlükteki ilgili kaydı okur.
Önceki oturum yeni oturum açılırken hemen günlüğe işlenir. Diğerleri arka plan senkronizasyonunda işlenir.
Günlük, harita gibi yalnızca bu bilgisayardaki `.git/info/exclude` ile git dışında tutulur.
`config.json` içinde `"worklog": false` ile kapatılır, `"worklog_recent"` bağlama girecek oturum sayısını belirler.

Claude Code'un kendi hafızası (CLAUDE.md ve otomatik hafıza) kararları ve kuralları tutar. Çalışma günlüğü ise ne yapıldığını tutar. İkisi birbirini tamamlar.

## Token geçmişi

Claude Code oturum kayıtlarını yaklaşık 30 gün sonra siler. Service001 her oturumun yalnızca sayılarını kalıcı olarak saklar.
Saklanan sayılar: proje, tarih, çağrı, işlenen token, bağlam medyanı, model dağılımı, keşif yükü, okuma ve kabuk payları.
Kayıt `~/.claude/cc-optimizer/history.db` dosyasındadır ve içerik saklanmaz.

Kayıt, oturum başında arka planda en fazla saatte bir güncellenir. Yalnızca yeni ve değişen oturumlar işlenir.
Kayıtları silinen oturumlar geçmişte kalır.

Her oturum kendi kaydına bakılarak etiketlenir:

| Etiket | Ne demek |
|---|---|
| Service001'li | Koruyucuların eklediği metin ilk model çağrısından önce var |
| Karışık | Koruyucular oturumun ortasında kurulmuş. Karşılaştırmaya alınmaz |
| Service001'siz | Koruyucuların izi yok |

```bash
.venv/Scripts/cc-optimizer history                     # hafta ve proje bazında token geçmişi
.venv/Scripts/cc-optimizer history --project KEIMORA   # tek proje
.venv/Scripts/cc-optimizer compare --project KEIMORA   # KEIMORA'da Service001'siz ve Service001'li oturumlar
.venv/Scripts/cc-optimizer compare                     # tüm projeler
.venv/Scripts/cc-optimizer compare --matched           # yalnızca iki tarafta da oturumu olan projeler
.venv/Scripts/cc-optimizer compare --markdown          # pull request veya ekip güncellemesi için Markdown tablo
```

`compare` artık varsayılan olarak bu geçmişi kullanır. Eski dosya tabanlı karşılaştırma için `--baseline data/baseline-2026-10-14-v2.json` verilir.

Raporu okumak için:

- **Güven düzeyi.** Her satır, oturumdan oturuma dalgalanmaya karşı denenir (oturumlar üzerinde bootstrap, %90 aralık, sabit tohum). Gürültüden ayırt edilemeyen değişim iyi ya da kötü diye değil `belirsiz` diye gösterilir. Bir tarafta 3'ten az oturum varsa hiçbir şey söylenemez. Aralık değişimin yanında yazılır.
- **Farklı projeler.** İki dönem büyük ölçüde farklı projeleri kapsıyorsa (örtüşme %50'nin altında) rapor, farkın işin kendisinden gelebileceği konusunda uyarır. `--matched` yalnızca iki tarafta da bulunan projeleri tutar; `--project AD` tek projeye bakar.
- **Korumaların dışarıda tuttukları.** Rapor, engelleyen korumaların ilk denemede bağlama girmesini önlediği token'ları olay günlüğünden ekler. Üst sınırdır, çünkü engellenen okuma daha küçük parçalarla yeniden denenebilir.
- `compare --json` aynı veriyi içerir (satır başına `confidence`, `interval`; ayrıca `projects`, `guard_effect`).

## Proje haritası

Her oturum başında, açılan klasör bir projeyse `.claude/project-map.md` dosyası üretilir veya güncellenir.
Klasörün proje sayılması için içinde git, `package.json`, `pyproject.toml` gibi bir işaret olmalı.

| İçerik | Nereden |
|---|---|
| Proje özeti | README'nin ilk paragrafı |
| Çalıştırma komutları | `package.json` scriptleri, `pyproject.toml` |
| Klasör yapısı | Git deposunda `git ls-files`, değilse klasör taraması. `node_modules`, `dist` gibi klasörler atlanır |
| Dosya açıklamaları | 150 dosyaya kadar projelerde her dosyanın baş yorumundan tek satır. Büyük projelerde klasörler dosya sayılarıyla |
| Sayfalar ve route'lar | "Şu sayfa hangi dosyada" sorusunun cevabı: route, dosya, bileşen ve sayfanın kullandıkları. Next, SvelteKit, Nuxt, Astro, React Router, Angular, Vue Router, Razor/MVC ve Blazor sayfaları. API için Express, NestJS, Flask, FastAPI, Django, Spring, ASP.NET, Laravel, Go, Rails |
| Arama indeksi | `.claude/project-index.md`: her route, sayfa ve üst seviye sembol için `isim  dosya:satır  tür`. Claude tek `grep` ile bulur |
| Bağımlılıklar ve merkezler | Her dosyanın hangi dosyaları içe aktardığı ve onu kimlerin içe aktardığı (tsconfig `paths` destekli JS/TS, Vue, Svelte, Python, Java, Kotlin, Go, C#), ayrıca en çok kodun bağlı olduğu dosya ve semboller. "Bunu değiştirirsem ne etkilenir" sorusunu cevaplar |

Haritanın kendisi bağlama eklenmez, yalnızca yolu eklenir. Bağlama eklenen her şey her çağrıda yeniden okunur. Dosya ise gerektiğinde bir kez okunur.
Harita yoksa, 6 saatten eskiyse, git'te yeni commit varsa veya dosya eklenip silindiyse yeniden üretilir.
Tespit düzenli ifadelerle yapılır, bir parser değildir: kaçırdığı olursa Claude normal arama yapar. İç içe route'lar üst route ile birleştirilmez.
Bağımlılıklar statik import'lardan gelir. Go, C#, Java ve Kotlin dosyaları paket ve namespace üzerinden bağlar: orada bir dosya, görebildiği bir kapsamda tek yerde tanımlı bir tipi (veya dışa açık Go fonksiyonunu) anıyorsa o dosyaya bağlanır. Rust, PHP ve Ruby için henüz bağımlılık kenarı yok. Dinamik import'lar ve yansıma görülmez. Sembol referansları ada göre sayılır: birden çok dosyada tanımlı veya çok genel adlar sayılmaz.
Git'e girmemesi için yalnızca bu bilgisayardaki `.git/info/exclude` dosyasına eklenir. Paylaşılan `.gitignore` değişmez.

```bash
.venv/Scripts/cc-optimizer map            # bu klasörün haritasını şimdi üret
.venv/Scripts/cc-optimizer map --print    # yazmadan göster
cc-optimizer impact src/lib/api.ts       # değişiklikten etkilenen dosyalar (tekilse sadece ad yeter)
cc-optimizer map --graph > graph.json    # dosyalar, import'lar, semboller, route'lar düz JSON olarak
```

**Zaten açık bir oturumda.** Harita normalde oturum başlarken üretilir. Koruyucular kurulmadan önce açılmış bir oturum için, yeniden başlatmadan iki yol var:

- Hook'lar etkin olduktan sonraki ilk mesaj haritayı bir kez duyurur (yoksa hook üretir). Hook'lar oturum açıkken kurulduysa, oturumun onları yeniden yüklemesi için Claude Code'un `/hooks` menüsünü bir kez açın.
- `/project-map` komutu (`install` ekler) harita ve index'i istediğiniz an yeniler ve Claude'a onları kullanmasını söyler. Claude'dan `cc-optimizer map --announce` komutunu çalıştırmasını ister; bunu terminalde kendiniz de çalıştırabilirsiniz.

Bağlama yalnızca yollar ve kullanım ipuçları girer, haritanın kendisi girmez.

`config.json` içinde `"project_map": false` ile kapatılır.

Projeye özel ayar için `.claude/cc-optimizer.json` yazılabilir (commit edilip ekiple paylaşılabilir):

```json
{
  "ignore": ["legacy/**", "*.generated.ts"],
  "routes": [
    {"files": "src/**/*.ts", "kind": "page", "regex": "registerPage\\('(?P<path>[^']+)', '(?P<name>\\w+)'"}
  ]
}
```

`ignore` dosyaları haritadan çıkarır. `routes` kendi route kalıbınızı ekler: regex'te `path` grubu şarttır, `method` ve `name` isteğe bağlıdır. Dosya değişince harita yenilenir.

## Dil

Çıktı İngilizce ve Türkçe desteklenir. Seçim sırası: `CC_OPTIMIZER_LANG` ortam değişkeni (`en` veya `tr`), `config.json` içinde `"language": "en" | "tr" | "auto"`, sistem dili (Türkçeyse Türkçe), yoksa İngilizce. Claude'a giden metinler her zaman İngilizcedir.
`compare` çıktısındaki "İlk değişiklikten önceki keşif" satırı etkisini ölçer. Başlangıçta medyan yaklaşık 10,6 bin token.

## Model yönlendirme

Claude Code, hook'ların ana sohbetin modelini değiştirmesine izin vermiyor.
Konuşmanın ortasında model değiştirmek de pahalı, çünkü her modelin önbelleği ayrı ve konuşmanın tamamı yeniden işleniyor.
Bu yüzden yönlendirme iki noktada yapılır.

| Parça | Ne yapar |
|---|---|
| Varsayılan model | Yeni sohbetler Sonnet ile başlar (`--model sonnet`). Bütün bir oturum zor olacaksa `/model opus` kullanılır. Kaldırınca önceki model geri gelir |
| Alt ajanlar | Aşağı: `scout` (Haiku) arama ve okuma, `worker` (Sonnet) net tanımlı kodlama. Yukarı: `architect` (Opus) mimari, zor hata ayıklama ve riskli çok dosyalı değişiklik, `expert` (Fable) en zor problemler. Alt ajan sıfırdan başladığı için önbellek kaybı olmaz |
| Oturum başı kuralları | Claude'a hangi işi hangi ajana vereceğini söyler. Yalnızca ajanlar kuruluysa eklenir |
| Keşif hatırlatması | Ana sohbet art arda 4 arama veya okuma yaptıktan sonraki çağrıda, keşfe devam edecekse scout'a (Haiku) devretmesini söyler. Keşfin çıktıları sohbette birikmez, yalnızca kısa rapor gelir. Her keşif dizisinde bir kez yapılır. Claude 3 hatırlatmayı uygulamazsa o oturumda durur. Alt ajanların kendi çağrılarına uygulanmaz. Eşik `routing_nudge_after` ayarıyla değişir |
| Varsayılan alt ajan modeli | Model belirtmeyen alt ajanlar Opus yerine bu modelle çalışır |
| İlk promptta model önerisi | Oturumun ilk promptunda, iş basitse Claude size `/model haiku` veya `/model sonnet` önerir. Geçişin ucuz olduğu tek an budur |

```bash
.venv/Scripts/cc-optimizer install --scope user --agents --subagent-model sonnet
```

İlk ölçümde tüketimin %95'i ana sohbetteydi ve alt ajanların 83 çağrısından 78'i Opus'u miras alıyordu.
En büyük etki, oturumu işe uygun modelle başlatmaktan gelir.
`model_routing` ve `model_advice` ayarları `config.json` içinden kapatılabilir.

## Beklemede: LLM API gateway

`src/llm_gateway`, kendi uygulamalarında LLM API'si kullanan kurumlar için yazılmış ölçüm proxy'sidir.
OpenAI ve Anthropic uyumlu istekleri olduğu gibi iletir ve token, maliyet ve gecikme kaydeder.
Şu anki öncelik değildir.

```bash
cp config.example.yaml config.yaml
.venv/Scripts/llm-gateway serve --config config.yaml --port 8080
.venv/Scripts/llm-gateway report --days 7
```

Sağlayıcı anahtarları `.env` dosyasından okunur. Bu dosya ve `config.yaml` git'e girmez.

## Test

```bash
.venv/Scripts/python -m pytest -q
```

## Yapı

```
src/cc_optimizer/
  transcripts.py  oturum kayıtlarını ayrıştırma
  analyze.py      kaynak, taşınan yük, kalibrasyon, senaryo
  report.py       metin raporu (İngilizce / Türkçe)
  compare.py      önce / sonra karşılaştırması
  hooks.py        bağlam koruyucuları (hook işleyicileri)
  statusline.py   durum satırı
  installer.py    ayar dosyasına kurma ve kaldırma
  guard_config.py koruyucu ayarları, oturum durumu, olay günlüğü
  shell_reads.py  kabuk komutlarında tam dosya basmayı bulma
  project_map.py  proje haritası
  code_index.py   sayfa, route ve sembol tespiti
  i18n.py         İngilizce / Türkçe metin seçimi
  doctor.py       sağlık kontrolü
  history.py      kalıcı token geçmişi
  worklog.py      proje çalışma günlüğü
  agents/         scout, worker, expert alt ajan tanımları
  cli.py          komut satırı
src/llm_gateway/  beklemedeki API gateway
tests/
```
