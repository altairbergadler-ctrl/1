package com.example.wpstore

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.View
import android.webkit.WebViewClient
import androidx.fragment.app.Fragment
import com.example.wpstore.databinding.FragmentCatalogBinding

class CatalogFragment : Fragment(R.layout.fragment_catalog) {

    @SuppressLint("SetJavaScriptEnabled")
    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        val binding = FragmentCatalogBinding.bind(view)
        binding.catalogWebView.webViewClient = WebViewClient()
        binding.catalogWebView.settings.javaScriptEnabled = true
        binding.catalogWebView.loadUrl(getString(R.string.catalog_url))
    }
}
