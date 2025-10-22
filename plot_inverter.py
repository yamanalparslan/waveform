import pandas as pd
import numpy as np
from scipy.signal import butter, filtfilt
import matplotlib.pyplot as plt

# CSV dosyasını oku
df = pd.read_csv("/Users/yamanalparslan/Desktop/projects/waveform/waveform.csv")

# Zaman ve sinyal
n_samples = len(df)
fs_expected = 300000.0  # Java kodundaki örnekleme frekansı
dt = 1.0 / fs_expected
t = np.arange(n_samples) * dt

vC_signal = df["vC"].to_numpy()

# Düşük geçiren filtre (SPWM pürüzlerini azaltmak için)
fc = 500  # Hz
b, a = butter(2, fc / (fs_expected / 2), btype='low')
vC_filtered = filtfilt(b, a, vC_signal)

# 220 V RMS ölçeğine çevir
vC_scaled = vC_filtered * (220.0 / np.max(np.abs(vC_filtered)))

plt.figure(figsize=(12,6))

# Alt grafik 1: Tüm sinyal
plt.subplot(2,1,1)
plt.plot(t, vC_scaled, label='Filtrelenmiş vC (220V ölçekli)', linewidth=2)
plt.title("Çıkış Gerilimi vC (Tüm Sinyal)")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.legend()
plt.grid(True)

# Alt grafik 2: İlk 2 periyot yakınlaştırma
period = 1.0 / 50.0  # 50 Hz
samples_per_period = int(fs_expected * period)
zoom_samples = samples_per_period * 2

plt.subplot(2,1,2)
plt.plot(t[:zoom_samples], vC_scaled[:zoom_samples], label='Filtrelenmiş vC (220V ölçekli)', linewidth=2)
plt.title("Çıkış Gerilimi vC (İlk 2 Periyot Yakınlaştırma)")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()
