// /static/js/script.js

document.addEventListener('DOMContentLoaded', () => {
  // Mobile Navigation Toggle
  const hamburger = document.querySelector('.hamburger');
  const navLinks = document.querySelector('.nav-links');
  if (hamburger && navLinks) {
    hamburger.addEventListener('click', () => {
      navLinks.classList.toggle('active');
      hamburger.classList.toggle('active');
    });
  }

  // Mobile Bottom Navigation
  const nav = document.querySelector('.mobile-bottom-nav');
  function updateMobileNav() {
    if (window.innerWidth <= 768) {
      nav.style.display = 'flex';
    } else {
      nav.style.display = 'none';
    }
  }
  window.addEventListener('load', updateMobileNav);
  window.addEventListener('resize', updateMobileNav);

  // Highlight Active Nav Item
  const navItems = document.querySelectorAll('.nav-item');
  const currentPath = window.location.pathname;
  navItems.forEach(item => {
    const href = item.getAttribute('href');
    if (currentPath === href) {
      item.classList.add('active');
    } else {
      item.classList.remove('active');
    }
    item.addEventListener('click', () => {
      navItems.forEach(i => i.classList.remove('active'));
      item.classList.add('active');
    });
  });

  // Mobile Bottom Nav Tooltip Toggle
  if (window.matchMedia('(max-width: 768px)').matches) {
    const navItems = document.querySelectorAll('.mobile-bottom-nav .nav-item');
    navItems.forEach(item => {
      item.addEventListener('touchend', e => {
        e.preventDefault();
        const tooltip = item.querySelector('.tooltip');
        const href = item.getAttribute('href');
        console.log(`Tapped item: ${href}, Tooltip exists: ${!!tooltip}`);
        if (href === '/login') {
          console.log('Navigating to login');
          window.location.href = href;
          return;
        }
        if (tooltip) {
          navItems.forEach(other => {
            const otherTooltip = other.querySelector('.tooltip');
            if (otherTooltip && other !== item) {
              otherTooltip.style.display = 'none';
            }
          });
          const isVisible = tooltip.style.display === 'block';
          tooltip.style.display = isVisible ? 'none' : 'block';
          console.log(`Toggled ${href} tooltip to: ${tooltip.style.display}`);
          if (isVisible) {
            console.log(`Navigating to ${href}`);
            window.location.href = href;
          }
        } else {
          console.log(`No tooltip, navigating to ${href}`);
          window.location.href = href;
        }
      });
    });
  }

  // Tooltip Hiding
  const tooltips = document.querySelectorAll('.nav-links .tooltip, .mobile-bottom-nav .tooltip');
  tooltips.forEach(tooltip => {
    setTimeout(() => {
      tooltip.style.display = 'none';
    }, 15000);
  });

  // Location Input Toggle
  const locationBtn = document.querySelector('.location-btn');
  const locationInput = document.querySelector('.location-input');
  if (locationBtn && locationInput) {
    locationBtn.addEventListener('click', () => {
      if (!locationInput.style.display || locationInput.style.display === 'none') {
        locationInput.style.display = 'block';
        locationInput.focus();
      } else {
        locationInput.style.display = 'none';
      }
    });
  }

  // Filter Buttons
  document.querySelectorAll('.deal-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.deal-filter').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('selected-deal-type').value = btn.dataset.value;
      document.getElementById('search-form').submit();
    });
  });

  document.querySelectorAll('.category-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.category-filter').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('selected-category').value = btn.dataset.value;
      document.getElementById('search-form').submit();
    });
  });

  // Service Worker Registration & Push Subscription
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    return new Uint8Array([...rawData].map(c => c.charCodeAt(0)));
  }

  async function subscribeUser() {
    try {
      const reg = await navigator.serviceWorker.ready;
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('Permission not granted');

      const vapidKey = window.vapidPublicKey;
      if (!vapidKey) throw new Error('Missing VAPID public key!');
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey)
      });
      const resp = await fetch('/subscribe', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub)
      });
      if (!resp.ok) throw new Error(`Subscribe API failed: ${resp.status}`);
      console.log('[Push] Subscribed');
    } catch (err) {
      console.error('[Push] Subscription error:', err);
    }
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/service-worker.js')
      .then(reg => console.log('[SW] registered', reg.scope))
      .catch(err => console.error('[SW] registration failed:', err));
  }

  // Notification Prompt
  const PROMPT_KEY = 'swapsphere_notif_prompted';
  const banner = document.getElementById('notify-prompt');
  if (USER_LOGGED_IN && !USER_SUBSCRIBED && !localStorage.getItem(PROMPT_KEY)) {
    document.querySelectorAll('.listing-card-link').forEach(link => {
      link.addEventListener('click', () => {
        banner.style.display = 'block';
      });
    });
  }

  document.getElementById('notify-yes').addEventListener('click', () => {
    subscribeUser();
    localStorage.setItem(PROMPT_KEY, 'yes');
    banner.remove();
  });

  document.getElementById('notify-no').addEventListener('click', () => {
    localStorage.setItem(PROMPT_KEY, 'no');
    banner.remove();
  });

  // Auto-Scroll Carousel
  const track = document.querySelector('.multi-carousel-track');
  const items = Array.from(document.querySelectorAll('.multi-carousel-item'));
  const count = items.length;
  if (count) {
    const PLAN_WEIGHTS = { Diamond: 5, Gold: 4, Silver: 3, Bronze: 2, Free: 1 };
    const plans = items.map(item => item.dataset.plan || 'Free');
    let weightedCycle = [];
    plans.forEach((plan, idx) => {
      const weight = PLAN_WEIGHTS[plan] || 1;
      for (let i = 0; i < weight; i++) {
        weightedCycle.push(idx);
      }
    });

    let currentCycleIdx = 0;
    let itemsPerView = 1;
    let itemWidth = 0;

    function calculateLayout() {
      const containerWidth = document.querySelector('.multi-carousel-container').clientWidth;
      if (window.innerWidth >= 1024) itemsPerView = 4;
      else if (window.innerWidth >= 768) itemsPerView = 3;
      else if (window.innerWidth >= 480) itemsPerView = 2;
      else itemsPerView = 1;
      itemWidth = containerWidth / itemsPerView;
      scrollToIndex(weightedCycle[currentCycleIdx]);
    }

    function scrollToIndex(itemIdx) {
      const maxStart = count - itemsPerView;
      const start = Math.min(itemIdx, maxStart);
      track.style.transform = `translateX(${-start * itemWidth}px)`;
    }

    function autoScroll() {
      currentCycleIdx = (currentCycleIdx + 1) % weightedCycle.length;
      scrollToIndex(weightedCycleIdx[currentCycleIdx]);
    }

    let autoInterval;
    function startAuto() { autoInterval = setInterval(autoScroll, 4000); }
    function stopAuto() { clearInterval(autoInterval); }

    window.addEventListener('resize', calculateLayout);
    const container = document.querySelector('.multi-carousel-container');
    container.addEventListener('mouseenter', stopAuto);
    container.addEventListener('mouseleave', startAuto);

    calculateLayout();
    startAuto();
  }

  // Lazy Loading Images
  function lazyLoadImages() {
    const images = document.querySelectorAll('img[loading="lazy"]');
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const img = entry.target;
          const src = img.dataset.src;
          if (src) {
            img.src = src;
            img.removeAttribute('data-src');
          }
          observer.unobserve(img);
        }
      });
    }, { rootMargin: '200px' });
    images.forEach(img => observer.observe(img));
  }
  lazyLoadImages();

  // Impression and Click Tracking
  const carouselItems = Array.from(document.querySelectorAll('.multi-carousel-item'));
  const gridLinks = Array.from(document.querySelectorAll('.listing-card-link'));
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const el = entry.target;
        const id = el.dataset.id;
        const src = el.dataset.source || 'grid';
        fetch('/api/track_impression', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ listing_id: id, source: src })
        });
        observer.unobserve(el);
      }
    });
  }, { threshold: 0.2 });

  carouselItems.concat(gridLinks).forEach(el => observer.observe(el));

  function handleClick(e) {
    const el = e.currentTarget;
    const id = el.dataset.id;
    const src = el.dataset.source || 'grid';
    fetch('/api/track_click', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ listing_id: id, source: src })
    });
  }

  carouselItems.concat(gridLinks).forEach(el => {
    el.addEventListener('click', handleClick);
  });

  // Wishlist Button
  document.querySelectorAll('.wishlist-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      const id = btn.dataset.listingId;
      const icon = btn.querySelector('i.fas');
      const wasWishlisted = icon.classList.contains('wishlisted');
      icon.classList.toggle('wishlisted');
      btn.setAttribute('aria-pressed', wasWishlisted ? 'false' : 'true');
      btn.classList.remove('animate');
      void btn.offsetWidth;
      btn.classList.add('animate');

      fetch('/api/wishlist/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ listing_id: id })
      })
      .then(res => res.json())
      .then(data => {
        if (!data.success) throw new Error('Wishlist toggle failed');
        document.getElementById('wishlist-count').textContent = data.total;
        const m = document.getElementById('wishlist-count-mobile');
        if (m) m.textContent = data.total;
      })
      .catch(err => {
        console.error(err);
        icon.classList.toggle('wishlisted', wasWishlisted);
        btn.setAttribute('aria-pressed', wasWishlisted ? 'true' : 'false');
      });
    });
  });

  // Proposal Status Update
  let lastAction = null;
  async function updateProposalStatus(id, status) {
    try {
      const res = await fetch(`/proposals/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status })
      });
      const data = await res.json();
      if (!data.success) {
        return showToast('Update failed', false);
      }

      const card = document.querySelector(`.proposal-card[data-id="${id}"]`);
      const badge = card.querySelector('.status-badge');
      const prev = badge.textContent;
      badge.textContent = status;
      badge.className = `status-badge status-${status}`;
      card.querySelectorAll('.proposal-actions button').forEach(b => b.disabled = true);

      lastAction = { id, prev };
      showToast(`Marked "${status}"`, true);
    } catch (e) {
      console.error(e);
      showToast('Network error', false);
    }
  }

  function showToast(msg, canUndo) {
    const toast = document.getElementById('undo-toast');
    document.getElementById('undo-message').textContent = msg;
    document.getElementById('undo-btn').style.display = canUndo ? 'inline' : 'none';
    toast.style.display = 'block';
    clearTimeout(toast.hideTimer);
    toast.hideTimer = setTimeout(() => toast.style.display = 'none', 5000);
  }

  document.getElementById('undo-btn').addEventListener('click', () => {
    if (lastAction) {
      updateProposalStatus(lastAction.id, lastAction.prev);
      lastAction = null;
    }
    document.getElementById('undo-toast').style.display = 'none';
  });

  document.querySelectorAll('.accept-proposal').forEach(btn => {
    btn.addEventListener('click', () => updateProposalStatus(btn.dataset.id, 'accepted'));
  });
  document.querySelectorAll('.decline-proposal').forEach(btn => {
    btn.addEventListener('click', () => updateProposalStatus(btn.dataset.id, 'declined'));
  });
  document.querySelectorAll('.negotiate-proposal').forEach(btn => {
    btn.addEventListener('click', () => updateProposalStatus(btn.dataset.id, 'negotiated'));
  });

  // Hero Carousel
  function initCarousel() {
    const items = document.querySelectorAll('.carousel-item');
    const dots = document.querySelectorAll('.dot');
    const prev = document.querySelector('.control-prev');
    const next = document.querySelector('.control-next');
    let idx = 0, total = items.length;
    function show(n) {
      items.forEach((it, i) => it.classList.toggle('active', i === n));
      dots.forEach((d, i) => d.classList.toggle('active', i === n));
      idx = n;
    }
    dots.forEach((d, i) => d.addEventListener('click', () => show(i)));
    prev.addEventListener('click', () => show((idx - 1 + total) % total));
    next.addEventListener('click', () => show((idx + 1) % total));
    setInterval(() => show((idx + 1) % total), 8000);
  }
  initCarousel();
});