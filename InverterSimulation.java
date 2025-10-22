import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;

public class InverterSimulation {
    // Konfigürasyon
    static final double Vdc = 48.0;        // DC bus (V)
    static final double fOut = 50.0;       // Çıkış sinüs frekansı (Hz)
    static final double fCarrier = 10000;  // Üçgen (carrier) frekansı (Hz) - anahtarlama
    static final double modulationIndex = 0.9; // 0..1
    static final double simTime = 0.02;    // Simülasyon süresi (s) (ör. 1 periods = 1/50 = 0.02s)
    static final double fs = 200000.0;     // Örnekleme frekansı (Hz) - yüksek olmalı
    // LC filtre ve yük (basit model)
    static final double L = 0.001;         // Indüktans (H)
    static final double C = 10e-6;         // Kapasitans (F)
    static final double Rload = 10.0;      // Yük direnci (Ohm)

    public static void main(String[] args) throws IOException {
        double dt = 1.0 / fs;
        int steps = (int) Math.ceil(simTime / dt);

        // Durum değişkenleri
        double iL = 0.0;  // endüktör akımı
        double vC = 0.0;  // kapasitör gerilimi (filtre çıkışı)
        double halfV = Vdc / 2.0;

        PrintWriter pw = new PrintWriter(new FileWriter("waveform.csv"));
        pw.println("time,ua_switch,ub_switch,vd_ab,il,vC,load_v");

        for (int n = 0; n < steps; n++) {
            double t = n * dt;

            // Referans ve carrier sinyalleri
            double ref = modulationIndex * Math.sin(2.0 * Math.PI * fOut * t); // -1..1
            double carrier = Math.sin(2.0 * Math.PI * fCarrier * t);            // -1..1 (üçgen yerine sin kullandım basitlik için)

            // SPWM karşılaştırma -> üst leg (a) ve alt leg (b)
            // H-köprü farkı vd_ab = Va - Vb
            double va = (ref >= carrier) ? halfV : -halfV; // üst yarı köprü
            double vb = (-ref >= carrier) ? halfV : -halfV; // alt yarı köprü. (alternatif strateji)
            // Bu yaklaşım tek fazlı bipolar SPWM sağlar. İstendiğinde unipolar vs bipolar değiştirilebilir.

            double vd_ab = va - vb; // H-köprünün anlık çıkışu (ideal anahtarlama)

            // Basit RLC diferansiyel denklemlerinin Euler entegrasyonu (state-space):
            // L * diL/dt = vd_ab - vC
            // C * dvC/dt = iL - vC/Rload

            double diL_dt = (vd_ab - vC) / L;
            double dvC_dt = (iL - vC / Rload) / C;

            // Euler integrasyonu
            iL += diL_dt * dt;
            vC += dvC_dt * dt;

            double loadV = vC; // yük üzerinde filtre sonrası gerilim

            // Kaydet
            pw.printf("%.9f,%.3f,%.3f,%.3f,%.6f,%.6f,%.6f%n", t, va, vb, vd_ab, iL, vC, loadV);
        }

        pw.close();
        System.out.println("Simülasyon tamamlandı. waveform.csv oluşturuldu.");
    }
}
