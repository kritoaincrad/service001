# Yol Haritası: Claude Code Token Optimizasyonu

## Ürün tanımı

Claude Code kullanan geliştiricilerin ve ekiplerin token tüketimini, iş kalitesini düşürmeden azaltan bir araç seti.
Önce tüketimi ölçer, sonra Claude Code'un hook mekanizmasıyla bağlamı şişiren davranışları yakalar.
Hepsi kullanıcının bilgisayarında çalışır, hiçbir içerik dışarı gönderilmez.

## Neden bu öncelikler: ölçüm bulguları

İlk ölçüm 2026-10-14 tarihinde, bu bilgisayardaki 22 Claude Code oturumu ve 70 alt ajan üzerinde yapıldı.
Toplam 13.944 model çağrısı ve 5,5 milyar işlenmiş token incelendi.

| Bulgu | Değer |
|---|---|
| Tüketimde cache'ten yeniden okunan bağlamın payı | %98,3 |
| Çağrı başına medyan bağlam | 362 bin token |
| Otomatik sıkıştırmadan önceki medyan bağlam | 949 bin token |
| Sıkıştırma 400 bin token'da tetiklenseydi tahmini tasarruf | %53 |
| Bağlam yükünde Write ile baştan yazılan dosyaların payı | yaklaşık %19 |
| Bağlam yükünde dosya okumalarının payı | yaklaşık %17 |
| Bağlam yükünde kabuk komutu çıktılarının payı | yaklaşık %12 |
| Oturum açılışındaki sabit yük, medyan | 41,5 bin token |
| Bağlam yükünde kullanıcı mesajlarının payı | yaklaşık %2 |

Çıkan sonuçlar:

- **Asıl maliyet, uzun oturumlarda büyüyen bağlamın her çağrıda yeniden okunması.** Tek seferlik büyüklük değil, bir içeriğin kaç çağrı boyunca taşındığı önemli.
- **Kullanıcı promptunu optimize etmek tüketimin yaklaşık %2'sine dokunur.** Ayrıca Claude Code hook'ları promptu yeniden yazamıyor, yalnızca bağlam ekleyebiliyor.
- **En büyük kaldıraçlar oturum uzunluğu, dosyaların baştan yazılması ve büyük okumalar.**

## İlkeler

- **Önce ölç, sonra müdahale et.** Her koruyucu, devreye girmeden önce ve sonra aynı analizle ölçülür.
- **Engellemek yerine yönlendir.** Hook'lar mümkün olduğunda Claude'a gerekçe ve daha ucuz bir yol gösterir.
- **Kalite kaybı kabul edilmez.** Bir koruyucu işi uzatıyor veya hataya yol açıyorsa kapatılır.
- **İçerik bilgisayardan çıkmaz.** Ekip raporlarına yalnızca sayılar gider.

## Fazlar

### Faz 1: Ölçüm — tamamlandı

- Claude Code oturum kayıtlarını okuyup token'ların nereye gittiğini raporlayan komut satırı aracı.
- Her içeriğin boyutu ve kaç çağrı boyunca taşındığı, araç ve komut bazında kırılım.
- Gereksiz tekrar okumalar, sınırsız büyük okumalar, en pahalı oturumlar.
- Daha erken sıkıştırma için senaryo hesabı.
- Tahminler gerçek bağlam büyümesine göre kalibre edilir. Rapor, gerçek tüketimin ne kadarını açıkladığını gösterir.

### Faz 2: Bağlam koruyucuları (3-4 hafta)

Sıra, ölçülen etkiye göredir. Her madde ayrı açılıp kapatılabilir.

1. **Bağlam uzunluğu.** Durum satırında bağlam doluluğu ve oturumun maliyeti gösterilir. Eşik aşılınca Claude'a "işi özetle, yeni oturum öner" bağlamı eklenir. Ekip için `/autocompact` eşiği önerilir.
2. **Dosyaların baştan yazılması.** Var olan büyük bir dosyaya Write çağrısı yapılırsa PreToolUse hook'u reddeder ve gerekçe olarak Edit kullanmasını söyler. Bash heredoc ile dosya yazımı için de aynı uyarı verilir.
3. **Büyük okumalar.** Sınır verilmeden büyük bir dosya okunmak istenirse önce Grep ile yer bulması veya aralık vermesi önerilir.
4. **Gürültülü kabuk çıktıları.** Uzun çıktılar PostToolUse hook'u ile hatalar ve son satırlar korunarak kısaltılır. Tam çıktı bir dosyaya yazılır ve yolu Claude'a verilir.
5. **Arka plan görevlerinin tekrar tekrar okunması.** Aynı görev çıktısını sık okuma tespit edilip uyarılır.

Belgelere göre doğrulanması gerekenler: PreToolUse içinde girdi değiştirmenin Read ve Bash için çalıştığı belgede örneklenmemiş. Uygulamadan önce test edilecek.

**Durum (2026-10-14):** 1. ve 2. maddeler yazıldı, test edildi ve bu projeye yerel olarak kuruldu.

- Bağlam uyarısı, durum satırı ve oturum başı kuralları çalışıyor. Claude Code'un yerleşik `autoCompactWindow` ayarı kurulum aracından isteğe bağlı olarak ayarlanabiliyor.
- Baştan yazma koruyucusu varsayılan olarak engellemiyor, yönlendiriyor. Hook çalıştığında model içeriği zaten üretmiş oluyor. Engellemek o anki maliyeti geri almıyor, Edit ile tekrar denendiği için artırıyor. Kalıcı kazanç, Claude'un oturumun geri kalanında davranışını değiştirmesinden geliyor.
- Başlangıç ölçümü `data/baseline-2026-10-14.json` dosyasına kaydedildi. Claude Code eski kayıtları 30 gün sonra sildiği için karşılaştırma bu dosyaya dayanacak.
- Koruyucular 2026-10-14 tarihinde kullanıcı genelinde kuruldu.
- 3. madde de yazıldı ve kuruldu. Okumada engellemek, yönlendirmekten iyidir, çünkü dosya henüz bağlama girmemiştir.
  İlk ölçüme göre 6 bin token eşiğinde 71 okuma, sonraki çağrılarda 223 milyon token taşıdı. Reddetmenin fazladan çağrı maliyeti 22 milyon token.
  Dosyanın yarısı yine okunsa bile net kazanç yaklaşık 89 milyon token, toplam tüketimin yaklaşık %1,6'sı.
  Ret mesajı dosyanın satır numaralı taslağını içerir, böylece Claude Grep'e gerek kalmadan doğru aralığı okuyabilir.
- 4. madde ölçümle değişti. 3 bin token'ı aşan kabuk çıktılarının kazanç payının yaklaşık üçte ikisi gürültülü log değil.
  Bunlar `cat` ve `sed` ile basılan dosya içerikleri, yani Read koruyucusunu atlatan okumalar.
  - **Tam dosya basan komutlar:** `cat` ve `Get-Content` gibi komutlar artık büyük okuma koruyucusuna tabi. Geçmiş 7.282 komutun 28'inde tetiklenirdi, yanlış yakalama görülmedi.
  - **Uzun çıktıları kısaltma:** Claude Code'un yerleşik `bashOutputMaxChars` ayarı bunu yapıyor ve tam çıktıyı dosyaya yazıyor. Bu yüzden ayrı hook yazılmadı, kurulum aracına seçenek olarak eklendi.
- 5. madde ölçümle kapatıldı. Arka plan görevlerini okumanın ayrı bir aracı yük getirmiyor, `TaskStop` toplamda 23 bin token. Bu görevlerin çıktısı kabuk komutlarıyla okunuyor ve kabuk koruyucularının kapsamında.
- `compare` komutu eklendi. Başlangıç ölçümünü kurulumdan sonraki oturumlarla, çağrı başına değerler ve paylar üzerinden karşılaştırıyor.
  Başlangıç ölçümü model dağılımını da içerecek şekilde aynı 22 oturumla `data/baseline-2026-10-14-v2.json` olarak yeniden üretildi.
- `doctor` komutu eklendi. Kurulumu, Python yolunu, hook'ların gerçekten çalışıp çalışmadığını ve ayarları kontrol ediyor. Hook'lar artık her çalıştığında zaman damgası bırakıyor.
- Kalıcı token geçmişi eklendi: `history.db`. Claude Code kayıtları silse de her oturumun sayıları kalıyor.
  - **Etiketleme:** Oturumlar Service001'li ya da Service001'siz olarak kendi kayıtlarındaki izden etiketleniyor. Ortasında kurulum yapılan oturumlar karışık sayılıp karşılaştırmadan çıkarılıyor.
  - **`compare`:** Artık bu geçmişi kullanıyor ve proje bazında çalışıyor (`--project MYAPP`).
- Proje çalışma günlüğü eklendi: `.claude/worklog.md`.
  - **Ne yazılıyor:** Her oturumun istekleri, değişen dosyaları, komutları ve sonucu. Kayıtlardan kural tabanlı çıkarılıyor, token harcamıyor.
  - **Bağlama ne giriyor:** Yeni sohbette ve sıkıştırmadan sonra yalnızca son işlerin kısa özeti ve dosyanın yolu.
  - **Neden:** Kullanıcının en önemli isteklerinden biri, Claude'un bir projede neler yapıldığını unutmaması.
- Sıradaki: bir iki hafta veri toplayıp `compare` çalıştırmak. Faz 3 (paketleme) şimdilik gerekmiyor, araç yalnızca tek kullanıcı için.

**Çıkış kriteri:** Aynı kişinin benzer işlerinde önce ve sonra ölçümünde tüketim belirgin şekilde düşmüş, iş tamamlama süresi uzamamış olmalı.

### Faz 2b: Model yönlendirme — kuruldu (2026-10-14)

- Hook'lar ana sohbetin modelini değiştiremiyor. Konuşma ortasında model değiştirmek önbelleği sıfırladığı için zaten pahalı.
- Ölçüm: tüketimin %95'i ana sohbette. Alt ajan çağrılarının 83'ünden 78'i model belirtmeden Opus'u miras alıyordu.
- Yapılanlar:
  - Haiku, Sonnet ve Fable alt ajanları.
  - Oturum başında iş dağıtma kuralları.
  - Model belirtilmeyen alt ajanlar için varsayılan Sonnet.
  - İlk promptta, geçişin ucuz olduğu anda model önerisi.
- Ölçülecek: alt ajanlarda model dağılımı, öneriden sonra oturumların hangi modelle sürdüğü.
- İlk ölçüm (2026-10-14, tek projede 1 oturum, 260 çağrı): alt ajan hiç kullanılmadı, oturum baştan sona Opus'la sürdü. Oturum başındaki kurallar yetmedi.
- Düzeltme: keşif hatırlatması eklendi.
  - **Ne yapıyor:** Ana sohbet art arda 4 arama veya okuma yapınca Claude'a keşfi scout'a vermesi söyleniyor.
  - **Neden 4:** Geçmiş oturumlarda dördüncü çağrıdan sonraki keşif, ana sohbet bağlamının %7,3'ünü (391 milyon token) taşıyordu.
  - **Nasıl ölçülecek:** Alt ajan çağrıları tür, model ve hatırlatmadan sonra yapılıp yapılmadıklarıyla birlikte olay günlüğüne yazılıyor.
- Ana model kararı (2026-10-14, kullanıcı): varsayılan Sonnet, zor işlerde yukarı devretme. Tüketimin %95'i ana sohbette olduğu için asıl kaldıraç bu.
  - **Yukarı devretme:** Zor işler için `architect` (Opus) alt ajanı eklendi.
  - **Kurallar:** Yönlendirme kuralları hem aşağı hem yukarı devretmeyi anlatıyor. İlk promptta ileri düzey işler için `/model opus` öneriliyor.
  - **Değerlendirme:** Bir hafta sonra `compare` ile ölçülecek. Kalite düşerse `uninstall` veya `install --model opus` ile geri dönülecek.
- Proje haritası eklendi. Ölçüm: oturum başı keşif tüketimin %2,2'si, medyan 10,6 bin token. Önceki oturumlarda okunan dosyaları yeniden okuma %1,8.
  - **Nerede:** Harita `.claude/project-map.md` dosyasında, oturum başında güncelleniyor.
  - **Bağlama ne giriyor:** Yalnızca dosyanın yolu. İçerik eklenseydi her çağrıda taşınırdı.
  - **Nasıl ölçülecek:** `compare` çıktısındaki "İlk değişiklikten önceki keşif" satırıyla.

### Faz 3: Paketleme (2 hafta)

- Hook'lar ve rapor komutu bir Claude Code eklentisi olarak paketlenir. Ekip yerel bir eklenti pazarından tek komutla kurar.
- Durum satırı eklenti ile verilemediği için kurulum betiği kullanıcı ayarlarına ekler.

### Faz 4: Ekip ölçümü (3-4 hafta)

- Her geliştiricinin bilgisayarında üretilen özet, içerik olmadan yalnızca sayılarla toplanır.
- Ekip paneli: kişi, proje ve hafta bazında tüketim ve koruyucuların etkisi.

### Faz 5: Kurumsal sürüm (sürekli)

- Kurum genelinde merkezi ayarlar ile politika dağıtımı.
- Bütçe ve kullanım limitlerine göre uyarılar, aylık yönetici raporu.

## Riskler

- **Kayıt formatı resmi değil.** Claude Code oturum kayıtlarının formatı sürümden sürüme değişebilir. Karşı önlem: ayrıştırıcı hatalara toleranslı, her sürümde testlerle kontrol.
- **Koruyucular işi yavaşlatabilir.** Karşı önlem: her biri ayrı açılıp kapatılır ve etkisi ölçülür.
- **Erken sıkıştırma ayrıntı kaybettirir.** Karşı önlem: eşik ekip bazında, ölçüme göre seçilir.
- **Claude Code aynı özellikleri kendisi ekleyebilir.** Karşı önlem: değeri ekip ölçümü ve kurumsal raporlamaya taşımak.

## Beklemede: API gateway

`src/llm_gateway` altındaki ölçüm gateway'i, kendi uygulamalarında LLM API'si kullanan kurumlar içindir.
Şu anki önceliğimiz değil. Bu tür bir müşteri talebi gelirse oradan devam edilebilir.
