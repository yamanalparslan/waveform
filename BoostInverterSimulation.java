import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;

public class BoostInverterSimulation {
    // Konfigürasyon
    static final double Vdc = 12.0;           // DC besleme
    static final double VacRMS = 220.0;       // Hedef AC RMS
    static final double fOut = 60.0;          // Çıkış frekansı 50 Hz
    static final double fCarrier = 30000.0;   // Carrier frekansı 20 kHz
    static final double modulationIndex = 0.95;
    static final double simTime = 0.2;        // Simülasyon süresi 0.2 s
    static final double fs = 200000.0;        // Örnekleme frekansı

    // LC filtre ve yük (boost ve 220V çıkış için ayarlandı)
    static final double L = 0.01;            // Indüktans 10 mH
    static final double C = 470e-6;          // Kapasitans 470 µF
    static final double Rload = 100.0;      
    static final double ESR_L = 0.1;
    static final double ESR_C = 0.05;

    public static void main(String[] args) {
        try {
            runSimulation();
        } catch (IOException e) {
            System.err.println("Dosya yazma hatası: " + e.getMessage());
            e.printStackTrace();
        }
    }

    private static void runSimulation() throws IOException {
        double dt = 1.0 / fs;
        int steps = (int) Math.ceil(simTime / dt);

        double iL = 0.0;
        double vC = 0.0;
        double Vpeak = VacRMS * Math.sqrt(2);  // AC pik değeri

        String outputPath = "waveform.csv";
        PrintWriter pw = new PrintWriter(new FileWriter(outputPath));
        pw.println("time,ua_switch,ub_switch,vd_ab,il,vC,load_v");

        System.out.println("Simülasyon başlatılıyor...");
        System.out.printf("Toplam adım sayısı: %d%n", steps);
        System.out.printf("Zaman adımı dt: %.9f s%n", dt);
        System.out.printf("Kesim frekansı (LC): %.2f Hz%n", 1.0 / (2 * Math.PI * Math.sqrt(L * C)));

        long startTime = System.currentTimeMillis();

        for (int n = 0; n < steps; n++) {
            double t = n * dt;

            // Sinüsoidal referans ve carrier
            double ref = modulationIndex * Math.sin(2.0 * Math.PI * fOut * t) * Vpeak;
            double carrier = Math.sin(2.0 * Math.PI * fCarrier * t) * Vpeak;

            // SPWM anahtarlama
            double va = (ref >= carrier) ? Vdc / 2 : -Vdc / 2;
            double vb = (-ref >= carrier) ? Vdc / 2 : -Vdc / 2;
            double vd_ab = va - vb;

            // Euler integrasyonu ile LC filtre
            double diL_dt = (vd_ab - vC - iL * ESR_L) / L;
            double dvC_dt = (iL - vC / Rload - vC * ESR_C / Rload) / C;

            iL += diL_dt * dt;
            vC += dvC_dt * dt;

            double loadV = vC;

            pw.printf("%.9f,%.3f,%.3f,%.3f,%.6f,%.6f,%.6f%n", 
                     t, va, vb, vd_ab, iL, vC, loadV);

            if (n % (steps / 10) == 0) {
                System.out.printf("İlerleme: %.0f%%%n", (n * 100.0 / steps));
            }
        }

        pw.close();
        long endTime = System.currentTimeMillis();
        System.out.printf("Simülasyon tamamlandı. Süre: %.2f s%n", (endTime - startTime) / 1000.0);
        System.out.printf("Dosya oluşturuldu: %s%n", outputPath);
        System.out.printf("Toplam veri noktası: %d%n", steps);
    }
}
