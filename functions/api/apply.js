export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    const formData = await request.formData();

    const name = (formData.get('name') || '').toString().trim();
    const email = (formData.get('email') || '').toString().trim();
    const phone = (formData.get('phone') || '').toString().trim();
    const position = (formData.get('position') || '').toString().trim();
    const message = (formData.get('message') || '').toString().trim();
    const resumeFile = formData.get('resume');

    // Basic validation
    if (!name || !email || !position) {
      return jsonResponse({ success: false, error: 'Missing required fields.' }, 400);
    }

    if (!resumeFile || typeof resumeFile === 'string') {
      return jsonResponse({ success: false, error: 'Resume file is required.' }, 400);
    }

    const MAX_SIZE = 10 * 1024 * 1024; // 10MB
    if (resumeFile.size > MAX_SIZE) {
      return jsonResponse({ success: false, error: 'Resume file is too large (max 10MB).' }, 400);
    }

    const allowedTypes = [
      'application/pdf',
      'application/msword',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ];
    if (resumeFile.type && !allowedTypes.includes(resumeFile.type)) {
      return jsonResponse({ success: false, error: 'Resume must be a PDF or Word document.' }, 400);
    }

    // Check env vars are present
    if (!env.RESUMES_BUCKET) return jsonResponse({ success: false, error: 'DEBUG: RESUMES_BUCKET binding missing.' }, 500);
    if (!env.RESEND_API_KEY) return jsonResponse({ success: false, error: 'DEBUG: RESEND_API_KEY not set.' }, 500);
    if (!env.FROM_EMAIL)     return jsonResponse({ success: false, error: 'DEBUG: FROM_EMAIL not set.' }, 500);
    if (!env.NOTIFY_EMAIL)   return jsonResponse({ success: false, error: 'DEBUG: NOTIFY_EMAIL not set.' }, 500);
    if (!env.R2_PUBLIC_URL)  return jsonResponse({ success: false, error: 'DEBUG: R2_PUBLIC_URL not set.' }, 500);

    // Build a unique, safe object key for R2
    const timestamp = Date.now();
    const safeName = name.replace(/[^a-zA-Z0-9]+/g, '_').slice(0, 50);
    const originalExt = (resumeFile.name.split('.').pop() || 'pdf').toLowerCase();
    const objectKey = `resumes/${timestamp}_${safeName}.${originalExt}`;

    // Upload resume to R2
    try {
      await env.RESUMES_BUCKET.put(objectKey, resumeFile.stream(), {
        httpMetadata: { contentType: resumeFile.type || 'application/octet-stream' }
      });
    } catch (r2err) {
      return jsonResponse({ success: false, error: `DEBUG: R2 upload failed — ${r2err.message}` }, 500);
    }

    const resumeUrl = `${env.R2_PUBLIC_URL}/${objectKey}`;

    // Compose notification email
    const emailHtml = `
      <h2>New Job Application — TWB Legal Search</h2>
      <p><strong>Position:</strong> ${escapeHtml(position)}</p>
      <p><strong>Name:</strong> ${escapeHtml(name)}</p>
      <p><strong>Email:</strong> ${escapeHtml(email)}</p>
      <p><strong>Phone:</strong> ${escapeHtml(phone || 'Not provided')}</p>
      <p><strong>Note:</strong><br>${escapeHtml(message || 'None').replace(/\n/g, '<br>')}</p>
      <p><strong>Resume:</strong> <a href="${resumeUrl}">${resumeUrl}</a></p>
      <hr>
      <p style="color:#888;font-size:12px;">Submitted ${new Date().toISOString()}</p>
    `;

    let emailResponse;
    try {
      emailResponse = await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.RESEND_API_KEY}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          from: env.FROM_EMAIL,
          to: env.NOTIFY_EMAIL,
          reply_to: email,
          subject: `New Application: ${position} — ${name}`,
          html: emailHtml
        })
      });
    } catch (fetchErr) {
      return jsonResponse({ success: false, error: `DEBUG: Fetch to Resend failed — ${fetchErr.message}` }, 500);
    }

    if (!emailResponse.ok) {
      const errText = await emailResponse.text();
      return jsonResponse({
        success: false,
        error: `DEBUG: Resend returned ${emailResponse.status} — ${errText}`
      }, 502);
    }

    return jsonResponse({ success: true });

  } catch (err) {
    return jsonResponse({ success: false, error: `DEBUG: Unexpected error — ${err.message}` }, 500);
  }
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
