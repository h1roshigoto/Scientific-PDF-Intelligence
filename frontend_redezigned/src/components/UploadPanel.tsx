export function UploadPanel() {
  return (
    <details className="surface upload-panel">
      <summary>Загрузить PDF</summary>
      <p className="muted upload-content">
        Сервер пока не принимает PDF через сайт. Документы появляются здесь после обработки на сервере.
      </p>
    </details>
  );
}
