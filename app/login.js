const API_URL = `/api`;

// Función auxiliar para obtener datos del formulario
function getFormData() {
    const formData = new URLSearchParams();
    formData.append('username', document.getElementById('username').value);
    formData.append('password', document.getElementById('password').value);
    return formData;
}

// Lógica de LOGIN
document.getElementById('loginBtn').addEventListener('click', async () => {
    try {
        const response = await fetch(`${API_URL}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: getFormData()
        });

        if (response.ok) {
            const data = await response.json();
            localStorage.setItem('token', data.access_token);
            window.location.href = 'index.html';
        } else {
            alert('Usuario o contraseña incorrectos');
        }
    } catch (err) { alert('Error de conexión'); }
});
