(function(){
    let visitorId = localStorage.getItem('visitor_id');
    if (!visitorId) {
        visitorId = self.crypto.randomUUID();
        localStorage.setItem('visitor_id', visitorId);
    }

    const deviceInfo = navigator.userAgent;

    setInterval(() => {
        fetch('/api/heartbeat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ 
                visitorId: visitorId, 
                currentPage: window.location.pathname,
                deviceInfo: deviceInfo
            })
        }).catch(err => console.error('Heartbeat failed', err));
    }, 30000);
})();
