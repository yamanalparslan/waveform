import pandas as pd
import matplotlib.pyplot as plt

# CSV dosyasını yükle
df = pd.read_csv("waveform.csv")

# Zaman ekseni
t = df["time"]

# Grafik 1 – H-köprü çıkışı (anahtarlanmış sinyal)
plt.figure()
plt.plot(t, df["vd_ab"])
plt.title("H-Köprü Çıkışı (vd_ab)")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.grid(True)

# Grafik 2 – Filtre çıkışı
plt.figure()
plt.plot(t, df["vC"], label="vC (Filtre Çıkışı)")
plt.plot(t, df["load_v"], "--", label="Yük Gerilimi")
plt.title("Filtre ve Yük Gerilimleri")
plt.xlabel("Zaman (s)")
plt.ylabel("Gerilim (V)")
plt.legend()
plt.grid(True)

# Grafik 3 – Endüktör akımı
plt.figure()
plt.plot(t, df["il"])
plt.title("Endüktör Akımı (iL)")
plt.xlabel("Zaman (s)")
plt.ylabel("Akım (A)")
plt.grid(True)

plt.show()
