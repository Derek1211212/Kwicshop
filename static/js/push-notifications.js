// Register service worker
if ('serviceWorker' in navigator && 'PushManager' in window) {
    navigator.serviceWorker.register('/static/sw.js')
    .then(function(swReg) {
        console.log('Service Worker registered');
        window.swRegistration = swReg;
    })
    .catch(function(error) {
        console.error('Service Worker Error', error);
    });
}

// Subscribe to a specific store
async function subscribeToStore(storeId) {
    if (!window.swRegistration) {
        console.log('Service Worker not ready');
        return false;
    }
    
    try {
        const applicationServerKey = urlBase64ToUint8Array('{{ vapid_public_key }}');
        const options = { applicationServerKey, userVisibleOnly: true };
        const subscription = await swRegistration.pushManager.subscribe(options);
        
        // Send subscription to server
        const response = await fetch('/store/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                store_id: storeId,
                subscription: subscription
            })
        });
        const data = await response.json();
        if (data.success) {
            console.log('Subscribed to store notifications');
            return true;
        }
    } catch (err) {
        console.error('Failed to subscribe', err);
    }
    return false;
}

// Unsubscribe
async function unsubscribeFromStore(storeId) {
    // ... similar, send DELETE to server
}

// Helper: base64 to Uint8Array
function urlBase64ToUint8Array(base64String) {
    // ... standard conversion
}