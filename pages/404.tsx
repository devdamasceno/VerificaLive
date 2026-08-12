import Head from "next/head";
import Image from "next/image";
import Link from "next/link";
import styles from "@/styles/Home.module.css";

export default function NotFoundPage() {
  return (
    <>
      <Head>
        <title>404 | VerificaLive</title>
        <meta name="description" content="Pagina nao encontrada no VerificaLive." />
      </Head>

      <main className={styles.notFoundPage}>
        <section className={styles.notFoundCard}>
          <Image
            className={styles.notFoundLogo}
            src="/logo.png"
            alt="VerificaLive"
            width={172}
            height={58}
            priority
          />
          <span>Erro 404</span>
          <h1>Pagina nao encontrada</h1>
          <p>
            O endereco acessado nao existe ou foi movido. Volte para o painel e continue
            acompanhando o debate.
          </p>
          <Link href="/">Voltar para a home</Link>
        </section>
      </main>
    </>
  );
}
