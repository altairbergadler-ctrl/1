package com.example.wpstore

import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.example.wpstore.databinding.FragmentHomeBinding

class HomeFragment : Fragment(R.layout.fragment_home) {

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        val binding = FragmentHomeBinding.bind(view)
        binding.vpnToggleButton.setOnClickListener {
            val enabled = !binding.vpnToggleButton.isSelected
            binding.vpnToggleButton.isSelected = enabled
            binding.vpnToggleButton.text =
                if (enabled) getString(R.string.vpn_turn_off) else getString(R.string.vpn_turn_on)

            val statusText = if (enabled) R.string.vpn_enabled else R.string.vpn_disabled
            binding.vpnStatus.text = getString(statusText)
            Toast.makeText(requireContext(), getString(statusText), Toast.LENGTH_SHORT).show()
        }
    }
}
