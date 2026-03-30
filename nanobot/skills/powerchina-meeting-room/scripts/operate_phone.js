(async () => {
    const iframe = document.querySelector('#zwIframe');
    const doc = iframe.contentDocument || iframe.contentWindow.document;

    const setSelectByLabel = (selector, label) => {
        const el = doc.querySelector(selector);
        const option = [...el.options].find(opt => opt.text.trim() === label);
        el.value = option.value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));

        const txt = doc.querySelector(selector + '_txt');
        if (txt) {
            txt.value = label;
            txt.dispatchEvent(new Event('input', { bubbles: true }));
            txt.dispatchEvent(new Event('change', { bubbles: true }));
            txt.dispatchEvent(new Event('blur', { bubbles: true }));
        }
    };

    setSelectByLabel('#field0042', '%phone_flag%');
})();
