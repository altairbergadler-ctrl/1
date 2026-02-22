package com.example.wpstore

import android.os.Bundle
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.example.wpstore.databinding.FragmentChatBinding

class ChatFragment : Fragment(R.layout.fragment_chat) {

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        val binding = FragmentChatBinding.bind(view)

        val chats = listOf(
            "Поддержка магазина",
            "Скидки и акции",
            "Отзывы покупателей",
            "VIP-клиенты"
        )

        binding.availableChats.adapter =
            ArrayAdapter(requireContext(), android.R.layout.simple_list_item_1, chats)

        binding.registerButton.setOnClickListener {
            val token = binding.tokenInput.text?.toString().orEmpty()
            if (token.isBlank()) {
                Toast.makeText(requireContext(), R.string.chat_token_required, Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }

            Toast.makeText(requireContext(), R.string.chat_registered, Toast.LENGTH_SHORT).show()
        }
    }
}
