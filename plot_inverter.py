import pandas as pd
import numpy as np
from scipy.signal import butter, filtfilt
import matplotlib.pyplot as plt

df = pd.read_csv("/Users/yamanalparslan/Desktop/projects/waveform/waveform.csv")

print("İlk 5 satır:")
print(df.head())
print("\nSütun isimleri:", df.columns.tolist())

# Time sütununu kontrol et
print("\nİlk 10 time değeri:")
print(df["time"].head(10))

# Eğer time sütunu sorunluysa, yeniden oluştur
n_samples = len(df)
fs_expected = 200000.0  # Java kodundaki fs değeri
dt = 1.0 / fs_expected

# Yeni zaman dizisi oluştur
t = np.arange(n_samples) * dt

# Sinyal verisini al
signal = df["vC"].to_numpy()

print(f"\nÖrnekleme frekansı fs = {fs_expected} Hz")
print(f"dt = {dt:.15e} s")
print(f"Toplam örnek sayısı: {n_samples}")

# Düşük geçiren filtre
fc = 500  # Hz
b, a = butter(2, fc / (fs_expected/2), btype='low')
vC_filtered = filtfilt(b, a, signal)

plt.figure(figsize=(12,6))

# Alt grafik 1: Tüm sinyal
plt.subplot(2,1,1)
plt.plot(t, signal, label='Orijinal vC', alpha=0.5)
plt.plot(t, vC_filtered, label='Filtrelenmiş vC', linewidth=2)
plt.title("Filtreli Çıkış vC (Tüm Sinyal)")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.legend()
plt.grid(True)

# Alt grafik 2: İlk 2 periyot yakınlaştırma
plt.subplot(2,1,2)
period = 1.0 / 50.0  # 50 Hz için periyot
samples_per_period = int(fs_expected * period)
zoom_samples = samples_per_period * 2

plt.plot(t[:zoom_samples], signal[:zoom_samples], label='Orijinal vC', alpha=0.5)
plt.plot(t[:zoom_samples], vC_filtered[:zoom_samples], label='Filtrelenmiş vC', linewidth=2)
plt.title("Filtreli Çıkış vC (İlk 2 Periyot - Yakınlaştırma)")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()