const API_URL = `/api`;

// Al cargar la página, llenamos el selector del muro
window.onload = cargarListaMuro;
let entradas = []

// URL de objeto (blob) actualmente en uso en el visualizador.
// Lo guardamos para poder liberarlo (revokeObjectURL) al cargar contenido nuevo.
let currentObjectUrl = null;

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
