const API_URL = `/api`;

// Al cargar la página, llenamos el selector del muro
window.onload = cargarListaMuro;
let entradas = []

// URL de objeto (blob) actualmente en uso en el visualizador.
// Lo guardamos para poder liberarlo (revokeObjectURL) al cargar contenido nuevo.
let currentObjectUrl = null;

// Instancia de la gráfica Chart.js mostrada actualmente (si la hay).
// La guardamos para poder destruirla antes de dibujar otra y evitar fugas.
let currentChart = null;

// Función "envoltorio" para peticiones protegidas
async function secureFetch(url, options = {}) {
    const token = localStorage.getItem('token');

    // Configurar headers por defecto
    options.headers = {
        ...options.headers,
        'Authorization': `Bearer ${token}`
    };

    const response = await fetch(url, options);

    // Si el servidor dice 401, el token caducó o no sirve
    if (response.status === 401) {

        console.warn("Token expirado o inválido. Redirigiendo al login...");
        
        localStorage.removeItem('token');
        
        alert("Tu sesión ha expirado. Por favor, inicia sesión de nuevo.");
        
        window.location.href = 'login.html';
        
        throw new Error("401 Unauthorized");
    }
    return response;
}

/**
 * 1. CONSULTA A LA IA SOBRE CLIMA EN MADRID
 */
async function consultarIA() {
    const preguntaInput = document.getElementById("pregunta-ia");
    const resultadoDiv = document.getElementById("resultado-ia");
    
    if (!preguntaInput.value) return alert("Escribe una pregunta");

    resultadoDiv.innerHTML = "<em> Consultando a la IA, por favor espera... </em>";

    try {
        const formData = new FormData();
        formData.append("pregunta", preguntaInput.value);

        const response = await secureFetch(`${API_URL}/assistant/analisys`, {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            resultadoDiv.innerHTML = marked.parse(data.respuesta);
        } else {
            // Manejo de errores (ej: 429 Límite excedido)
            console.error(data.detail.error);
            resultadoDiv.innerText = "Error: " + (data.detail.message || "Error desconocido");
        }
    } catch (error) {
        console.error(error);
        resultadoDiv.innerText = "Error de conexión con el servidor.";
    } finally {
        console.log("Refrescando muro...");
        await new Promise(resolve => setTimeout(resolve, 250));
        await cargarListaMuro(); // Refrescar el selector automáticamente
    }
}

/**
 * 2. CONSULTA A LA IA PARTICULAR
 */
async function consultarIAparticular() {
    const preguntaInput = document.getElementById("pregunta-ia-particular");
    const resultadoDiv = document.getElementById("resultado-ia-particular");
    
    if (!preguntaInput.value) return alert("Escribe una pregunta");

    resultadoDiv.innerHTML = "<em> Consultando a la IA, por favor espera... </em>";

    try {
        const formData = new FormData();
        formData.append("pregunta", preguntaInput.value);

        const response = await secureFetch(`${API_URL}/assistant-alumno/analisys`, {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            resultadoDiv.innerHTML = marked.parse(data.respuesta);
        } else {
            // Manejo de errores (ej: 429 Límite excedido)
            console.error(data.detail.error);
            resultadoDiv.innerText = "Error: " + (data.detail.message || "Error desconocido");
        }
    } catch (error) {
        console.error(error);
        resultadoDiv.innerText = "Error de conexión con el servidor.";
    } finally {
        console.log("Refrescando muro...");
        await new Promise(resolve => setTimeout(resolve, 250));
        await cargarListaMuro(); // Refrescar el selector automáticamente
    }
}

/**
 * 3. SUBIR CONTENIDO AL MURO
 */
async function subirAlMuro() {
    const desc = document.getElementById("muro-desc").value;
    const content_type = document.getElementById("muro-content-type").value;
    const archivoInput = document.getElementById("muro-archivo");

    if (archivoInput.files.length === 0) return alert("Selecciona un archivo");

    const formData = new FormData();
    formData.append("content_type", content_type);
    formData.append("description", desc);
    formData.append("file", archivoInput.files[0]);

    try {
        const response = await secureFetch(`${API_URL}/wall/update`, {
            method: "POST",
            body: formData
        });

        if (response.ok) {
            alert("¡Contenido publicado!");
            document.getElementById("muro-desc").value = "";
            document.getElementById("muro-content-type").value = "";
            archivoInput.value = "";
            cargarListaMuro(); // Refrescar el selector automáticamente
        } else {
            alert("Error al subir contenido.");
        }
    } catch (error) {
        alert("Error de red al intentar subir.");
    }
}

/**
 * 4. CARGAR LISTA EN EL SELECTOR
 */
async function cargarListaMuro() {
    const selector = document.getElementById("selector-muro");

    //selector.innerHTML = '<option value="" disabled selected>Cargando contenido, espera...</option>';
    
    try {
        const response = await secureFetch(`${API_URL}/wall/query?limit=50`);
        entradas = await response.json();

        // 1. Limpiamos y añadimos la opción "por defecto" (vacía y deshabilitada)
        selector.innerHTML = '<option value="" disabled selected>-- Selecciona un elemento para gestionar --</option>';

        if (entradas.length === 0) {
            selector.innerHTML = '<option value="" disabled>El muro está vacío</option>';
            return;
        }

        entradas.forEach(item => {
            // Obviaremos textos con muestras de datos del contexto para la IA
            const esTexto = item.content_type && item.content_type.includes("text");
            const esAiContext = item.file_path && item.file_path.includes("ai_context/");

            if (esTexto && esAiContext) {
                return; 
            }

            const nombreFichero = item.file_path ? item.file_path.split('/').pop() : 'Sin nombre';
            
            const opt = document.createElement("option");
            opt.value = item._id;

            // Mostramos descripción, tipo y fecha para identificar cada entra fácilmente
            const fecha = new Date(item.timestamp).toLocaleString();
            const tipo = item.content_type || 'Sin tipo';
            opt.text = `${nombreFichero} - ${item.description || 'Sin descripción'} (${tipo}) - ${fecha}`;
            selector.appendChild(opt);
        });
    } catch (error) {
        console.error("Error al cargar muro:", error);
        selector.innerHTML = '<option value="" disabled>Error al cargar muro</option>';
    }
}

/**
 * 5. BORRAR ELEMENTO SELECCIONADO
 */
async function borrarDelMuro() {
    const id = document.getElementById("selector-muro").value;
    
    if (!id) return alert("Selecciona un elemento de la lista para borrar");

    const item = entradas.find(e => e._id === id);

    if (item && item.file_path && item.file_path.includes("ai_context/")) {
        alert("⚠️ Acción no permitida: No se pueden borrar archivos de contexto para la IA.");
        return;
    }

    if (!confirm("¿Estás seguro de que quieres eliminar este contenido para siempre?")) return;

    try {
        const response = await secureFetch(`${API_URL}/wall/${id}`, {
            method: "DELETE"
        });

        if (response.ok) {
            alert("Eliminado correctamente.");
            cargarListaMuro(); // Refrescar el selector automáticamente
        } else {
            alert("No se pudo borrar el elemento.");
        }
    } catch (error) {
        alert("Error de conexión.");
    }
}

/**
 * 6. VISUALIZAR ELEMENTO SELECCIONADO
 */
async function abrirContenido() {
    const id = document.getElementById("selector-muro").value;
    const visualizador = document.getElementById("visualizador");
    
    if (!id) return alert("Selecciona un elemento primero");

    // Liberamos el blob anterior (si lo hay) para evitar fugas de memoria
    if (currentObjectUrl) {
        URL.revokeObjectURL(currentObjectUrl);
        currentObjectUrl = null;
    }

    // Destruimos la gráfica anterior (si la hay) para evitar fugas y solapamientos
    if (currentChart) {
        currentChart.destroy();
        currentChart = null;
    }

    visualizador.innerHTML = "Cargando...";

    try {

        // Pedimos los datos (descripción, nombre archivo, etc.)
        const metaResponse = await secureFetch(`${API_URL}/wall/query/${id}`);
        const entry = await metaResponse.json();

        // Pedimos el archivo (con autenticación) para saber su contenido, tipo y nombre
        const response = await secureFetch(`${API_URL}/wall/view/${id}`);
        const contentType = response.headers.get("content-type");
        const nombreFichero = entry.file_path ? entry.file_path.split('/').pop() : 'Sin nombre';

        console.log("Meta detectada:", entry);
        console.log("Tipo de contenido detectado:", contentType);

        // CSV: lo detectamos por la extensión del fichero (más fiable que el
        // content-type, que al subir puede no ser "text/csv") o por el MIME.
        const esCSV = (nombreFichero && nombreFichero.toLowerCase().endsWith(".csv"))
            || (contentType && contentType.includes("csv"));

        if (esCSV) {
            const texto = await response.text();
            // Guardamos un blob para permitir descargar el CSV original
            const blob = new Blob([texto], { type: "text/csv" });
            currentObjectUrl = URL.createObjectURL(blob);
            mostrarCSV(visualizador, texto, nombreFichero, entry.description, currentObjectUrl);
            return;
        }

        // Para contenido de texto no necesitamos blob: lo leemos directamente.
        // Para el resto (imagen, audio, pdf, binarios) creamos un blob URL a partir
        // de la respuesta YA autenticada, y lo reutilizamos tanto para visualizar
        // como para descargar el fichero original.
        if (contentType && (contentType.includes("text") || contentType.includes("json") || contentType.includes("xml"))) {
            const texto = await response.text();
            const escaparHTML = (str) => {
                return str.replace(/[&<>"']/g, (m) => ({
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;'
                }[m]));
            };
            // Creamos también un blob para permitir la descarga del original
            const blob = new Blob([texto], { type: contentType });
            currentObjectUrl = URL.createObjectURL(blob);
            visualizador.innerHTML = `
                <p>Mostrando contenido de fichero de texto: ${nombreFichero} (${entry.description || contentType})</p>
                <div style="margin-bottom: 10px;">
                    <a href="${currentObjectUrl}" download="${nombreFichero}" style="padding:10px; background:grey; color:white; text-decoration:none; border-radius:5px;">
                        📥 Descargar original
                    </a>
                </div>
                <pre style="background:#f4f4f4; padding:10px; overflow-x:auto;">${escaparHTML(texto)}</pre>`;
            return;
        }

        // Para el resto de tipos, obtenemos los bytes crudos como blob
        const blob = await response.blob();
        currentObjectUrl = URL.createObjectURL(blob);

        const enlaceDescarga = `
            <div style="margin-bottom: 10px;">
                <a href="${currentObjectUrl}" download="${nombreFichero}" style="padding:10px; background:grey; color:white; text-decoration:none; border-radius:5px;">
                    📥 Descargar original
                </a>
            </div>`;

        if (contentType && contentType.startsWith("image")) {
            visualizador.innerHTML = `
                <p>Visualizando imagen: ${nombreFichero} (${entry.description || contentType})</p>
                ${enlaceDescarga}
                <img src="${currentObjectUrl}" style="max-width:100%; border-radius:5px;">`;
        } else if (contentType && contentType.startsWith("audio/")) {
            // Creamos un reproductor de audio integrado
            visualizador.innerHTML = `
                <p>Reproduciendo: ${nombreFichero} (${entry.description || 'Archivo de audio'})</p>
                ${enlaceDescarga}
                <audio controls style="width:100%;" preload="metadata">
                    <source src="${currentObjectUrl}" type="${contentType}">
                </audio>`;
        } else if (contentType && contentType.includes("pcap")) {
            visualizador.innerHTML = `
                <p>Archivo PCAP detectado ${nombreFichero}: (${entry.description || 'Captura de tráfico'})</p>
                <a href="${currentObjectUrl}" download="${nombreFichero}" style="padding:10px; background:grey; color:white; text-decoration:none; border-radius:5px;">
                    📥 Descargar para abrir en Wireshark
                </a>`;
        } else if (contentType && contentType.includes("pdf")) {
            visualizador.innerHTML = `
                <p>Documento PDF detectado: ${nombreFichero} (${entry.description || contentType})</p>
                <div style="margin-bottom: 10px;">
                    <a href="${currentObjectUrl}" target="_blank" style="padding:10px; background:blue; color:white; text-decoration:none; border-radius:5px;">
                        ↗️ Abrir en nueva pestaña del navegador
                    </a>
                    <a href="${currentObjectUrl}" download="${nombreFichero}" style="padding:10px; background:grey; color:white; text-decoration:none; border-radius:5px;">
                        📥 Descargar original
                    </a>
                </div>
                <embed src="${currentObjectUrl}" type="application/pdf" width="100%" height="600px" />
            `;
        } else {
            // Binario o formato no soportado para previsualización: al menos permitimos descargarlo
            visualizador.innerHTML = `
                <p>Archivo no visualizable (binario o formato no soportado): ${nombreFichero}</p>
                ${enlaceDescarga}`;
        }
    } catch (error) {
        console.error(error);
        visualizador.innerHTML = "Error al intentar abrir el archivo.";
    }
}

/**
 * 7. VISUALIZACIÓN DE CSV CON GRÁFICA AUTOMÁTICA
 *
 * Muestra el contenido del CSV y, cuando es posible, una gráfica generada a
 * partir de sus datos. El fichero original se puede descargar sin modificar.
 */
function escaparHTML(str) {
    return String(str).replace(/[&<>"']/g, (m) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[m]));
}

/**
 * Intenta convertir un valor de texto a número, admitiendo el separador
 * decimal europeo (coma) y los separadores de millar. Devuelve null si no
 * es un número válido.
 */
function aNumero(valor) {
    if (valor === null || valor === undefined) return null;
    let s = String(valor).trim();
    if (s === "") return null;
    const tienePunto = s.includes(".");
    const tieneComa = s.includes(",");
    if (tienePunto && tieneComa) {
        // Ambos separadores: el que aparece en ÚLTIMO lugar es el decimal.
        if (s.lastIndexOf(",") > s.lastIndexOf(".")) {
            // Formato europeo: punto=millar, coma=decimal (1.234,56)
            s = s.replace(/\./g, "").replace(",", ".");
        } else {
            // Formato anglosajón: coma=millar, punto=decimal (1,234.56)
            s = s.replace(/,/g, "");
        }
    } else if (tieneComa) {
        // Solo coma: la tratamos como separador decimal (1234,56)
        s = s.replace(",", ".");
    }
    // Solo punto (o sin separadores): se deja tal cual.
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
}

/** Comprueba si una columna (array de celdas) es mayoritariamente numérica. */
function columnaEsNumerica(valores) {
    let numericos = 0, total = 0;
    for (const v of valores) {
        if (v === null || v === undefined || String(v).trim() === "") continue;
        total++;
        if (aNumero(v) !== null) numericos++;
    }
    return total > 0 && (numericos / total) >= 0.8;
}

/** Comprueba si una columna parece contener fechas interpretables. */
function columnaEsFecha(valores) {
    let validas = 0, total = 0;
    for (const v of valores) {
        if (v === null || v === undefined || String(v).trim() === "") continue;
        total++;
        if (!isNaN(Date.parse(String(v).trim()))) validas++;
    }
    return total > 0 && (validas / total) >= 0.8;
}

const COLORES_GRAFICA = [
    "#3498db", "#e74c3c", "#27ae60", "#f39c12", "#9b59b6", "#16a085"
];
const MAX_SERIES = 3;      // nº máximo de columnas numéricas a representar
const MAX_PUNTOS = 1000;   // nº máximo de filas a dibujar (evita colgar el navegador)

function mostrarCSV(visualizador, texto, nombreFichero, descripcion, objectUrl) {
    // 1. Parseamos el CSV (cabeceras, comillas y separador auto-detectado)
    const parsed = Papa.parse(texto.trim(), {
        header: true,
        skipEmptyLines: true,
        dynamicTyping: false,
    });

    const filas = parsed.data || [];
    const columnas = (parsed.meta && parsed.meta.fields) ? parsed.meta.fields : [];

    const enlaceDescarga = `
        <div style="margin-bottom: 10px;">
            <a href="${objectUrl}" download="${escaparHTML(nombreFichero)}" style="padding:10px; background:grey; color:white; text-decoration:none; border-radius:5px;">
                📥 Descargar original
            </a>
        </div>`;

    const cabecera = `<p>Visualizando CSV: ${escaparHTML(nombreFichero)} (${escaparHTML(descripcion || "fichero CSV")})</p>${enlaceDescarga}`;

    // Si no hay columnas o filas aprovechables, mostramos solo el texto.
    if (columnas.length === 0 || filas.length === 0) {
        visualizador.innerHTML = `${cabecera}
            <p><em>No se ha podido interpretar el CSV como tabla; se muestra su contenido.</em></p>
            <pre style="background:#f4f4f4; padding:10px; overflow-x:auto;">${escaparHTML(texto)}</pre>`;
        return;
    }

    // 2. Decidimos qué columnas representar.
    const valoresPorColumna = {};
    for (const c of columnas) valoresPorColumna[c] = filas.map(f => f[c]);

    const columnasNumericas = columnas.filter(c => columnaEsNumerica(valoresPorColumna[c]));

    // Eje X: primera columna de fecha; si no hay, primera columna no numérica;
    // si tampoco, usaremos el número de fila.
    let colX = columnas.find(c => columnaEsFecha(valoresPorColumna[c]) && !columnasNumericas.includes(c));
    let tipo = "line";
    if (!colX) {
        const colCategoria = columnas.find(c => !columnasNumericas.includes(c));
        if (colCategoria) { colX = colCategoria; tipo = "bar"; }
    }

    // Series Y: columnas numéricas (excluyendo la de eje X si fuese numérica).
    const seriesY = columnasNumericas.filter(c => c !== colX).slice(0, MAX_SERIES);

    // Si no hay ninguna serie numérica, no podemos graficar.
    if (seriesY.length === 0) {
        visualizador.innerHTML = `${cabecera}
            <p><em>Este CSV no contiene columnas numéricas que se puedan representar en una gráfica. Se muestra su contenido.</em></p>
            ${tablaHTML(columnas, filas)}`;
        return;
    }

    // 3. Preparamos etiquetas y datos (limitando el nº de puntos).
    const filasUsadas = filas.slice(0, MAX_PUNTOS);
    const etiquetas = filasUsadas.map((f, i) => colX ? f[colX] : (i + 1));
    const datasets = seriesY.map((c, idx) => ({
        label: c,
        data: filasUsadas.map(f => aNumero(f[c])),
        borderColor: COLORES_GRAFICA[idx % COLORES_GRAFICA.length],
        backgroundColor: COLORES_GRAFICA[idx % COLORES_GRAFICA.length],
        fill: false,
        spanGaps: true,
    }));

    const aviso = filas.length > MAX_PUNTOS
        ? `<p class="subtitle">Mostrando las primeras ${MAX_PUNTOS} filas de ${filas.length}.</p>`
        : "";

    visualizador.innerHTML = `${cabecera}
        ${aviso}
        <div style="position: relative; height: 350px; margin-bottom: 15px;">
            <canvas id="grafica-csv"></canvas>
        </div>
        <details>
            <summary style="cursor:pointer;">Ver datos del CSV</summary>
            ${tablaHTML(columnas, filas)}
        </details>`;

    // 4. Dibujamos la gráfica con Chart.js.
    const ctx = document.getElementById("grafica-csv").getContext("2d");
    currentChart = new Chart(ctx, {
        type: tipo,
        data: { labels: etiquetas, datasets: datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                title: {
                    display: true,
                    text: colX ? `Datos por "${colX}"` : "Datos del CSV",
                },
            },
            scales: {
                x: { title: { display: !!colX, text: colX || "" } },
                y: { title: { display: true, text: seriesY.join(", ") } },
            },
        },
    });
}

/** Genera una tabla HTML (con escape) a partir de columnas y filas del CSV. */
function tablaHTML(columnas, filas, maxFilas = 100) {
    const cab = columnas.map(c => `<th style="border:1px solid #ddd; padding:4px;">${escaparHTML(c)}</th>`).join("");
    const cuerpo = filas.slice(0, maxFilas).map(f => {
        const celdas = columnas.map(c => `<td style="border:1px solid #ddd; padding:4px;">${escaparHTML(f[c] ?? "")}</td>`).join("");
        return `<tr>${celdas}</tr>`;
    }).join("");
    const nota = filas.length > maxFilas ? `<p class="subtitle">Mostrando las primeras ${maxFilas} filas de ${filas.length}.</p>` : "";
    return `<div style="overflow-x:auto;"><table style="border-collapse:collapse; font-size:0.85em;"><thead><tr>${cab}</tr></thead><tbody>${cuerpo}</tbody></table></div>${nota}`;
}
