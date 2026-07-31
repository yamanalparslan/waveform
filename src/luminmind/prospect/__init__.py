"""Kurulum öncesi verim analizi: kurulmamış GES'in üretim ve fizibilite modeli.

Bu paket `twin/` ile aynı fiziği kullanır ama farklı bir soruya cevap verir.
`twin/` "kurulu santral bugün ne üretmeliydi?" der ve geçmiş ölçümle kendini
kalibre eder. `prospect/` ise "buraya santral kurulsa 25 yılda ne üretir ve
para kazandırır mı?" der — ölçüm yoktur, dolayısıyla kalibrasyon da yoktur.

Ayrım pratikte iki şeyi değiştirir:

1. **Hava verisi kaynağı.** Tahmin yerine *tipik meteorolojik yıl* (TMY)
   kullanılır (`prospect/pvgis.py`). Tek bir gerçek yıl alınsaydı o yılın
   iyi/kötü geçmesi 25 yıllık NPV'ye doğrudan sızardı.
2. **Dizi geometrisi girdi değil, çıktıdır.** Kurulu tesiste `pv_arrays`
   satırı sahadaki gerçeği anlatır; burada panel yerleşimi çatı/arazi
   poligonundan *türetilir* (`prospect/geometry.py`, `prospect/layout.py`).

Üretim zinciri paylaşılır: yerleşimden çıkan `ArrayConfig` doğrudan
`twin.pipeline.run_chain`'e verilir. Fizik tek yerde durur; iki ayrı üretim
modeli tutmak ikisinin zamanla ayrışması demekti.
"""
